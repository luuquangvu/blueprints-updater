"""Tests for specialized blueprint provider behaviors and edge case URL formats."""

import json

from custom_components.blueprints_updater.const import SourceProviderType
from custom_components.blueprints_updater.providers import (
    BitbucketProvider,
    CodebergProvider,
    GenericProvider,
    GistProvider,
    GitHubProvider,
    GitLabProvider,
    HAForumProvider,
    ProviderRegistry,
)


def test_provider_identity():
    """Verify that each provider class correctly identifies its own provider type."""
    assert GitHubProvider().provider_type == SourceProviderType.GITHUB
    assert GistProvider().provider_type == SourceProviderType.GIST
    assert HAForumProvider().provider_type == SourceProviderType.HA_FORUM
    assert GitLabProvider().provider_type == SourceProviderType.GITLAB
    assert CodebergProvider().provider_type == SourceProviderType.CODEBERG
    assert BitbucketProvider().provider_type == SourceProviderType.BITBUCKET
    assert GenericProvider().provider_type == SourceProviderType.GENERIC


def test_github_provider_complex_urls():
    """Verify GitHubProvider handles non-standard routes and specialized Git ref formats.

    Includes verification of:
    - Non-file routes (tree views) returned without normalization.
    - CDN conversion for raw URLs using 'refs/heads'.
    - CDN conversion for UI URLs using 'refs/heads'.
    """
    provider = GitHubProvider()

    url = "https://github.com/user/repo/tree/main/blueprints"
    assert provider.normalize_url(url) == url

    url = "https://raw.githubusercontent.com/user/repo/refs/heads/main/bp.yaml"
    cdn_url = provider.get_cdn_url(url)
    assert cdn_url == "https://cdn.jsdelivr.net/gh/user/repo@main/bp.yaml"

    url = "https://github.com/user/repo/blob/refs/heads/main/bp.yaml"
    cdn_url = provider.get_cdn_url(url)
    assert cdn_url == "https://cdn.jsdelivr.net/gh/user/repo@main/bp.yaml"

    url = "https://github.com/user/repo/blob/main/raw/bp.yaml"
    assert (
        provider.normalize_url(url)
        == "https://raw.githubusercontent.com/user/repo/main/raw/bp.yaml"
    )


def test_ha_forum_metadata_parsing():
    """Verify HAForumProvider can extract metadata directly from the Forum's JSON response.

    Includes verification of:
    - Successful metadata extraction from valid Discourse topic JSON.
    - Fallback to hostname/topic_id when JSON content is malformed.
    """
    provider = HAForumProvider()
    url = "https://community.home-assistant.io/t/topic/123"

    content = json.dumps(
        {"slug": "awesome-blueprint", "post_stream": {"posts": [{"username": "expert_user"}]}}
    )
    metadata = provider.get_metadata(url, content=content)
    assert metadata["author"] == "expert_user"
    assert metadata["name"] == "awesome-blueprint"

    metadata = provider.get_metadata(url, content="invalid json")
    assert metadata["author"] == "community.home-assistant.io"


def test_ha_forum_content_extraction_robustness():
    """Verify HAForumProvider's resilience when parsing malformed or unexpected JSON structures.

    Includes verification of:
    - Handling non-list 'posts' structure.
    - Handling non-dictionary post items.
    - Successful extraction of YAML block from valid post structure.
    """
    provider = HAForumProvider()

    assert (
        provider.parse_content("", response_json={"post_stream": {"posts": "not a list"}}) is None
    )

    assert provider.parse_content("", response_json={"post_stream": {"posts": [None]}}) is None

    response_json = {
        "post_stream": {"posts": [{"cooked": "<pre><code>blueprint:\n  name: Test\n</code></pre>"}]}
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is not None
    assert "blueprint:" in content


def test_git_normalization_robustness():
    """Verify robustness of GitLab, Bitbucket, and Codeberg URL normalization.

    Ensures that already normalized URLs or invalid path structures do not cause errors.
    Includes verification of:
    - GitLab: Raw links preservation and short paths handling.
    - Codeberg: Raw links preservation and non-source paths handling.
    - Bitbucket: Raw links preservation and non-source paths handling.
    """
    gl = GitLabProvider()
    assert (
        gl.normalize_url("https://gitlab.com/user/repo/-/raw/main/bp.yaml")
        == "https://gitlab.com/user/repo/-/raw/main/bp.yaml"
    )
    assert gl.normalize_url("https://gitlab.com/too/short") == "https://gitlab.com/too/short"
    assert (
        gl.normalize_url("https://gitlab.com/user/repo/-/notblob/main/bp.yaml")
        == "https://gitlab.com/user/repo/-/notblob/main/bp.yaml"
    )

    cb = CodebergProvider()
    assert (
        cb.normalize_url("https://codeberg.org/user/repo/raw/branch/main/bp.yaml")
        == "https://codeberg.org/user/repo/raw/branch/main/bp.yaml"
    )
    assert cb.normalize_url("https://codeberg.org/too/short") == "https://codeberg.org/too/short"
    assert (
        cb.normalize_url("https://codeberg.org/user/repo/notsrc/branch/main/bp.yaml")
        == "https://codeberg.org/user/repo/notsrc/branch/main/bp.yaml"
    )

    bb = BitbucketProvider()
    assert (
        bb.normalize_url("https://bitbucket.org/user/repo/raw/master/bp.yaml")
        == "https://bitbucket.org/user/repo/raw/master/bp.yaml"
    )
    assert bb.normalize_url("https://bitbucket.org/too/short") == "https://bitbucket.org/too/short"
    assert (
        bb.normalize_url("https://bitbucket.org/user/repo/notsrc/master/bp.yaml")
        == "https://bitbucket.org/user/repo/notsrc/master/bp.yaml"
    )


def test_gist_metadata_normalization():
    """Verify that GistProvider handles /raw suffix when extracting metadata."""
    provider = GistProvider()

    url = "https://gist.github.com/author/gist_id"
    metadata = provider.get_metadata(url)
    assert metadata["author"] == "author"
    assert metadata["name"] == "gist_id"

    normalized_url = "https://gist.github.com/author/gist_id/raw"
    metadata_raw = provider.get_metadata(normalized_url)
    assert metadata_raw["author"] == "author"
    assert metadata_raw["name"] == "gist_id"


def test_gitlab_normalization_keeps_empty_path_unchanged():
    """Verify GitLab normalization is inert when there is no path to inspect."""
    assert GitLabProvider().normalize_url("https://gitlab.com") == "https://gitlab.com"


def test_provider_registry_returns_original_url_without_matching_provider():
    """Verify registry normalization returns invalid sources unchanged."""
    assert ProviderRegistry().normalize_url("not-a-url") == "not-a-url"


def test_ha_forum_metadata_prefers_post_containing_blueprint():
    """Verify forum metadata uses the post that actually contains blueprint YAML."""
    provider = HAForumProvider()
    url = "https://community.home-assistant.io/t/topic/123"
    content = json.dumps(
        {
            "slug": "target-blueprint",
            "post_stream": {
                "posts": [
                    None,
                    {"username": "intro_author", "cooked": "<p>No YAML here</p>"},
                    {
                        "username": "blueprint_author",
                        "cooked": "<pre><code>blueprint:\n  name: Real</code></pre>",
                    },
                ]
            },
        }
    )

    metadata = provider.get_metadata(url, content=content)

    assert metadata == {"author": "blueprint_author", "name": "target-blueprint"}
