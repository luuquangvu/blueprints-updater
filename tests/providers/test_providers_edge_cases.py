"""Tests for specialized blueprint provider behaviors and edge case URL formats."""

import orjson

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
    - Standard blob URLs normalized to raw githubusercontent URLs.
    """
    provider = GitHubProvider()

    url = "https://github.com/user/repo/tree/main/blueprints"
    assert provider.normalize_url(url) == url

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

    content = orjson.dumps(
        {"slug": "awesome-blueprint", "post_stream": {"posts": [{"username": "expert_user"}]}}
    ).decode("utf-8")
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
    _test_git_normalization_robustness(
        gl,
        "https://gitlab.com/user/repo/-/raw/main/bp.yaml",
        "https://gitlab.com/too/short",
        "https://gitlab.com/user/repo/-/notblob/main/bp.yaml",
    )
    cb = CodebergProvider()
    _test_git_normalization_robustness(
        cb,
        "https://codeberg.org/user/repo/raw/branch/main/bp.yaml",
        "https://codeberg.org/too/short",
        "https://codeberg.org/user/repo/notsrc/branch/main/bp.yaml",
    )
    bb = BitbucketProvider()
    _test_git_normalization_robustness(
        bb,
        "https://bitbucket.org/user/repo/raw/master/bp.yaml",
        "https://bitbucket.org/too/short",
        "https://bitbucket.org/user/repo/notsrc/master/bp.yaml",
    )


def _test_git_normalization_robustness(provider, normalized_url, short_url, non_source_url):
    """Verify providers preserve already normalized URLs and handle invalid paths gracefully."""
    assert provider.normalize_url(normalized_url) == normalized_url
    assert provider.normalize_url(short_url) == short_url
    assert provider.normalize_url(non_source_url) == non_source_url


def test_gist_metadata_normalization():
    """Verify that GistProvider handles /raw suffix when extracting metadata."""
    provider = GistProvider()

    _test_gist_metadata_normalization(provider, "https://gist.github.com/author/gist_id")
    normalized_url = "https://gist.github.com/author/gist_id/raw"
    _test_gist_metadata_normalization(provider, normalized_url)


def _test_gist_metadata_normalization(provider, url):
    """Verify that GistProvider correctly extracts metadata from both standard and /raw URLs."""
    metadata = provider.get_metadata(url)
    assert metadata["author"] == "author"
    assert metadata["name"] == "gist_id"


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
    content = orjson.dumps(
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
    ).decode("utf-8")

    metadata = provider.get_metadata(url, content=content)

    assert metadata == {"author": "blueprint_author", "name": "target-blueprint"}


def test_ha_forum_content_extraction_prefers_post0_and_ignores_replies():
    """Verify that HAForumProvider extracts blueprint from the initial post only."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                {
                    "username": "op_author",
                    "cooked": (
                        "<pre><code>blueprint:\n"
                        "  name: Original OP Blueprint\n"
                        "  domain: automation\n"
                        "</code></pre>"
                    ),
                },
                {
                    "username": "reply_user",
                    "cooked": (
                        "<pre><code>blueprint:\n"
                        "  name: Forked Reply Blueprint\n"
                        "  domain: automation\n"
                        "</code></pre>"
                    ),
                },
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is not None
    assert "Original OP Blueprint" in content
    assert "Forked Reply Blueprint" not in content


def test_ha_forum_content_extraction_prioritizes_valid_yaml_block_over_inline_code():
    """Verify HAForumProvider picks a valid blueprint mapping over inline references."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                {
                    "username": "op_author",
                    "cooked": (
                        "<p>Check this <code>blueprint: something</code> inline snippet.</p>"
                        "<pre><code>blueprint:\n"
                        "  name: Real Valid Blueprint\n"
                        "  domain: automation\n"
                        "  input: {}\n"
                        "</code></pre>"
                    ),
                }
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is not None
    assert content.strip() != "blueprint: something"
    assert "Real Valid Blueprint" in content


def test_ha_forum_content_extraction_falls_back_to_reply_when_post0_has_no_blueprint():
    """Verify that HAForumProvider falls back to reply blueprint when post 0 has none."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                {
                    "username": "op_author",
                    "cooked": "<p>This is a regular post without any blueprint code block.</p>",
                },
                {
                    "username": "reply_user",
                    "cooked": (
                        "<pre><code>blueprint:\n"
                        "  name: Fallback Reply Blueprint\n"
                        "  domain: automation\n"
                        "</code></pre>"
                    ),
                },
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is not None
    assert "Fallback Reply Blueprint" in content


def test_ha_forum_content_extraction_ignores_non_dict_posts():
    """Non-dict entries in posts should be ignored and return None if no valid posts."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                "not-a-dict",
                123,
                None,
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is None


def test_ha_forum_content_extraction_ignores_posts_without_cooked():
    """Posts missing cooked or with non-string cooked should return None if no valid posts."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                {"username": "user_without_cooked"},
                {"username": "user_with_int_cooked", "cooked": 42},
                {"username": "user_with_none_cooked", "cooked": None},
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is None


def test_ha_forum_content_extraction_returns_none_when_all_code_blocks_fail_parsing():
    """Verify that HAForumProvider returns None when all blueprint: blocks fail YAML parsing."""
    provider = HAForumProvider()
    response_json = {
        "post_stream": {
            "posts": [
                {
                    "username": "op_author",
                    "cooked": (
                        "<pre><code>blueprint: [unclosed list\n  name: Bad YAML\n</code></pre>"
                    ),
                }
            ]
        }
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is None


def test_ha_forum_skips_malformed_post_before_valid_blueprint():
    """Verify HAForumProvider selects the first valid post when earlier posts are malformed."""
    provider = HAForumProvider()
    response_json = {
        "slug": "awesome-blueprint",
        "post_stream": {
            "posts": [
                {
                    "username": "bad_author",
                    "cooked": (
                        "<pre><code>blueprint: [unclosed list\n  name: Bad YAML\n</code></pre>"
                    ),
                },
                {
                    "username": "good_author",
                    "cooked": (
                        "<pre><code>blueprint:\n"
                        "  name: Valid Fallback Blueprint\n"
                        "  domain: automation\n"
                        "  input: {}\n"
                        "</code></pre>"
                    ),
                },
            ]
        },
    }
    content = provider.parse_content("", response_json=response_json)
    assert content is not None
    assert "Valid Fallback Blueprint" in content

    meta = provider.get_metadata(
        "https://community.home-assistant.io/t/awesome-blueprint/12345",
        content=orjson.dumps(response_json).decode("utf-8"),
    )
    assert meta == {"author": "good_author", "name": "awesome-blueprint"}
