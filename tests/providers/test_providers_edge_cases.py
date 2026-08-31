"""Tests for specialized blueprint provider behaviors and edge case URL formats."""

from urllib.parse import urlparse

import orjson
import pytest

from custom_components.blueprints_updater.const import (
    SourceDomain,
    SourceProviderType,
)
from custom_components.blueprints_updater.providers import (
    BitbucketProvider,
    CodebergProvider,
    GenericProvider,
    GistProvider,
    GitHubProvider,
    GitLabProvider,
    HAForumProvider,
    ProviderRegistry,
    SourceProvider,
    registry,
)


def test_source_domain_enum() -> None:
    """Verify that SourceDomain enum members have expected domain strings."""
    assert SourceDomain.GITHUB == "github.com"
    assert SourceDomain.GITHUB_RAW == "raw.githubusercontent.com"
    assert SourceDomain.GIST == "gist.github.com"
    assert SourceDomain.HA_FORUM == "community.home-assistant.io"
    assert SourceDomain.GITLAB == "gitlab.com"
    assert SourceDomain.CODEBERG == "codeberg.org"
    assert SourceDomain.BITBUCKET == "bitbucket.org"


def test_provider_identity():
    """Verify that each provider class correctly identifies its own provider type."""
    assert GitHubProvider().provider_type == SourceProviderType.GITHUB
    assert GistProvider().provider_type == SourceProviderType.GIST
    assert HAForumProvider().provider_type == SourceProviderType.HA_FORUM
    assert GitLabProvider().provider_type == SourceProviderType.GITLAB
    assert CodebergProvider().provider_type == SourceProviderType.CODEBERG
    assert BitbucketProvider().provider_type == SourceProviderType.BITBUCKET
    assert GenericProvider().provider_type == SourceProviderType.GENERIC


def test_forum_provider_canonicalize_url() -> None:
    """Test HAForumProvider canonicalize_url removes slugs and normalizes topic URLs."""
    provider = HAForumProvider()
    url_full = "https://community.home-assistant.io/t/zigbee2mqtt-control-light-entity-including-press-turn-with-tuya-moes-smart-knob-ers-10tzbvk-aa/787779"
    url_updated_slug = "https://community.home-assistant.io/t/zigbee2mqtt-control-light-entity-including-press-turn-with-tuya-moes-smart-knob-ers-10tzbvk-aa-v1-1/787779"
    url_short = "https://community.home-assistant.io/t/787779"
    url_trailing_slash = "https://community.home-assistant.io/t/787779/"
    url_with_post = "https://community.home-assistant.io/t/slug/787779/1"
    url_www = "https://www.community.home-assistant.io/t/slug/787779"
    url_upper_scheme = "HTTPS://community.home-assistant.io/t/slug/787779"

    expected = "https://community.home-assistant.io/t/787779"
    assert provider.canonicalize_url(url_full) == expected
    assert provider.canonicalize_url(url_updated_slug) == expected
    assert provider.canonicalize_url(url_short) == expected
    assert provider.canonicalize_url(url_trailing_slash) == expected
    assert provider.canonicalize_url(url_with_post) == expected
    assert provider.canonicalize_url(url_www) == expected
    assert provider.canonicalize_url(url_upper_scheme) == expected


def test_forum_provider_is_same_source() -> None:
    """Test HAForumProvider is_same_source recognizes equivalent topic URLs."""
    provider = HAForumProvider()
    url1 = "https://community.home-assistant.io/t/old-slug/787779"
    url2 = "https://community.home-assistant.io/t/new-slug-v1-1/787779"
    url3 = "https://community.home-assistant.io/t/787779"
    url_www = "https://www.community.home-assistant.io/t/787779"
    url_different = "https://community.home-assistant.io/t/different-topic/12345"

    assert provider.is_same_source(url1, url2) is True
    assert provider.is_same_source(url1, url3) is True
    assert provider.is_same_source(url2, url3) is True
    assert provider.is_same_source(url1, url_www) is True
    assert provider.is_same_source(url1, url_different) is False


def test_github_provider_canonicalize_and_same_source() -> None:
    """Test GitHubProvider canonicalization and source comparison with edge cases."""
    provider = GitHubProvider()
    blob_url = "https://github.com/user/repo/blob/main/blueprint.yaml"
    raw_url = "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml"
    raw_web_url = "https://github.com/user/repo/raw/main/blueprint.yaml"
    diff_file = "https://github.com/user/repo/blob/main/other.yaml"
    diff_branch = "https://github.com/user/repo/blob/dev/blueprint.yaml"
    diff_repo = "https://github.com/user/other-repo/blob/main/blueprint.yaml"

    # Canonicalization trims whitespace and trailing slashes while preserving URL scheme
    assert provider.canonicalize_url(blob_url) == blob_url
    assert provider.canonicalize_url(raw_url) == raw_url
    assert provider.canonicalize_url(raw_web_url) == raw_web_url
    assert provider.canonicalize_url(f"  {blob_url}/  ") == blob_url

    # Equivalence checks normalize endpoints to verify source identity
    assert provider.is_same_source(blob_url, raw_url) is True
    assert provider.is_same_source(blob_url, raw_web_url) is True
    assert provider.is_same_source(blob_url, diff_file) is False
    assert provider.is_same_source(blob_url, diff_branch) is False
    assert provider.is_same_source(blob_url, diff_repo) is False


@pytest.mark.parametrize(
    ("url1", "url2", "expected"),
    [
        # HA Forum topic matching
        (
            "https://community.home-assistant.io/t/slug-a/99999",
            "https://community.home-assistant.io/t/99999/",
            True,
        ),
        (
            "https://community.home-assistant.io/t/slug-a/99999",
            "https://community.home-assistant.io/t/88888",
            False,
        ),
        # GitHub matching
        (
            "https://github.com/org/repo/blob/master/automation.yaml",
            "https://raw.githubusercontent.com/org/repo/master/automation.yaml",
            True,
        ),
        # Generic matching & whitespace/slashes
        ("https://example.com/bp.yaml", "https://example.com/bp.yaml", True),
        ("  https://example.com/bp.yaml/  ", "https://example.com/bp.yaml", True),
        ("https://example.com/a.yaml", "https://example.com/b.yaml", False),
        # Provider boundary (e.g. cross-domain comparison)
        (
            "https://community.home-assistant.io/t/slug-a/99999",
            "https://github.com/org/repo/blob/master/automation.yaml",
            False,
        ),
        # None handling
        (None, None, True),
        ("https://example.com/bp.yaml", None, False),
    ],
)
def test_registry_are_same_source(url1: str | None, url2: str | None, expected: bool) -> None:
    """Test ProviderRegistry are_same_source across diverse providers and boundaries."""
    assert registry.are_same_source(url1, url2) is expected


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


def test_is_same_source_hostname_case_gist() -> None:
    """GistProvider.is_same_source must be insensitive to hostname case."""
    provider = GistProvider()
    lower = "https://gist.github.com/user/abc123"
    upper = "https://GIST.GITHUB.COM/user/abc123"
    mixed = "https://Gist.GitHub.Com/user/abc123"
    assert provider.is_same_source(lower, upper) is True
    assert provider.is_same_source(lower, mixed) is True


def test_is_same_source_hostname_case_gitlab() -> None:
    """GitLabProvider.is_same_source must be insensitive to hostname case."""
    provider = GitLabProvider()
    lower = "https://gitlab.com/user/repo/-/blob/main/bp.yaml"
    upper = "https://GITLAB.COM/user/repo/-/blob/main/bp.yaml"
    assert provider.is_same_source(lower, upper) is True


def test_is_same_source_hostname_case_codeberg() -> None:
    """CodebergProvider.is_same_source must be insensitive to hostname case."""
    provider = CodebergProvider()
    lower = "https://codeberg.org/user/repo/src/branch/main/bp.yaml"
    upper = "https://CODEBERG.ORG/user/repo/src/branch/main/bp.yaml"
    assert provider.is_same_source(lower, upper) is True


def test_is_same_source_hostname_case_bitbucket() -> None:
    """BitbucketProvider.is_same_source must be insensitive to hostname case."""
    provider = BitbucketProvider()
    lower = "https://bitbucket.org/user/repo/src/master/bp.yaml"
    upper = "https://BITBUCKET.ORG/user/repo/src/master/bp.yaml"
    assert provider.is_same_source(lower, upper) is True


def test_is_same_source_www_alias_generic() -> None:
    """GenericProvider.is_same_source treats www. as an alias for the bare host."""
    provider = GenericProvider()
    bare = "https://example.com/blueprint.yaml"
    www = "https://www.example.com/blueprint.yaml"
    assert provider.is_same_source(bare, www) is True


def test_registry_are_same_source_hostname_case_variants() -> None:
    """registry.are_same_source handles hostname-case and www. variants end-to-end."""
    reg = ProviderRegistry()

    # GitLab via registry
    assert (
        reg.are_same_source(
            "https://gitlab.com/u/r/-/blob/main/bp.yaml",
            "https://GITLAB.COM/u/r/-/blob/main/bp.yaml",
        )
        is True
    )

    # Codeberg via registry
    assert (
        reg.are_same_source(
            "https://codeberg.org/u/r/src/branch/main/bp.yaml",
            "https://CODEBERG.ORG/u/r/src/branch/main/bp.yaml",
        )
        is True
    )

    # Bitbucket via registry
    assert (
        reg.are_same_source(
            "https://bitbucket.org/u/r/src/master/bp.yaml",
            "https://BITBUCKET.ORG/u/r/src/master/bp.yaml",
        )
        is True
    )

    # Gist via registry (hostname-case)
    assert (
        reg.are_same_source(
            "https://gist.github.com/user/abc123",
            "https://GIST.GITHUB.COM/user/abc123",
        )
        is True
    )

    # Generic www. alias via registry
    assert (
        reg.are_same_source(
            "https://example.com/bp.yaml",
            "https://www.example.com/bp.yaml",
        )
        is True
    )


@pytest.mark.parametrize(
    ("url", "expected_fragment"),
    [
        ("https://[::1]:8080/blueprint.yaml", "[::1]:8080"),
        ("https://[::1]/blueprint.yaml", "[::1]"),
    ],
)
def test_canonicalize_url_ipv6_idempotent(url: str, expected_fragment: str) -> None:
    """canonicalize_url must bracket IPv6 literals and remain stable on repeated calls."""
    provider = GenericProvider()
    canonical = provider.canonicalize_url(url)
    assert expected_fragment in canonical
    assert provider.canonicalize_url(canonical) == canonical  # idempotent


def test_gist_normalize_url_normalizes_hostname_on_already_raw_url() -> None:
    """GistProvider.normalize_url must normalize hostname even when URL is already /raw."""
    provider = GistProvider()
    upper_raw = "https://GIST.GITHUB.COM/user/abc123/raw"
    result = provider.normalize_url(upper_raw)
    assert result == "https://gist.github.com/user/abc123/raw"
    # Idempotent: normalizing an already-lowercase raw URL returns it unchanged.
    lower_raw = "https://gist.github.com/user/abc123/raw"
    assert provider.normalize_url(lower_raw) == lower_raw


def test_ha_forum_canonicalize_url_non_topic_fallback_normalizes_hostname() -> None:
    """HAForumProvider.canonicalize_url non-topic fallback must normalize hostname via super()."""
    provider = HAForumProvider()
    non_topic_upper = "https://WWW.COMMUNITY.HOME-ASSISTANT.IO/categories"
    result = provider.canonicalize_url(non_topic_upper)
    assert result == "https://community.home-assistant.io/categories"


def test_github_get_metadata_empty_path_returns_fallback() -> None:
    """GitHubProvider.get_metadata must return 'unknown' for empty path URLs."""
    provider = GitHubProvider()
    result = provider.get_metadata("https://github.com")
    assert result["author"] == "unknown"
    # 'blueprint.yaml' is the fallback filename; _strip_yaml_extension removes .yaml
    assert result["name"] == "blueprint"


def test_dedicated_providers_strip_explicit_ports() -> None:
    """Dedicated HTTPS providers must remove explicit ports from normalized URLs."""
    cases = (
        (
            GitHubProvider(),
            "https://github.com:443/user/repo/blob/main/bp.yaml",
            "https://raw.githubusercontent.com/user/repo/main/bp.yaml",
        ),
        (
            GistProvider(),
            "https://gist.github.com:443/user/abc123",
            "https://gist.github.com/user/abc123/raw",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:443/t/topic/123",
            "https://community.home-assistant.io/t/123.json",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com:443/user/repo/-/blob/main/bp.yaml",
            "https://gitlab.com/user/repo/-/raw/main/bp.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:443/user/repo/src/branch/main/bp.yaml",
            "https://codeberg.org/user/repo/raw/branch/main/bp.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:443/user/repo/src/master/bp.yaml",
            "https://bitbucket.org/user/repo/raw/master/bp.yaml",
        ),
    )

    for provider, source_url, expected_url in cases:
        assert provider.normalize_url(source_url) == expected_url


def test_normalize_url_removes_fragments() -> None:
    """Normalized fetching URLs must not contain client-side fragments."""
    cases = (
        (
            GenericProvider(),
            "https://example.com/blueprint.yaml#section",
            "https://example.com/blueprint.yaml",
        ),
        (
            GitHubProvider(),
            "https://github.com/user/repo/blob/main/blueprint.yaml#section",
            "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml",
        ),
        (
            GistProvider(),
            "https://gist.github.com/user/abc123#section",
            "https://gist.github.com/user/abc123/raw",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com/user/repo/-/blob/main/bp.yaml#section",
            "https://gitlab.com/user/repo/-/raw/main/bp.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org/user/repo/src/branch/main/bp.yaml#section",
            "https://codeberg.org/user/repo/raw/branch/main/bp.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org/user/repo/src/master/bp.yaml#section",
            "https://bitbucket.org/user/repo/raw/master/bp.yaml",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io/t/topic/123#section",
            "https://community.home-assistant.io/t/123.json",
        ),
    )

    for provider, source_url, expected_url in cases:
        assert provider.normalize_url(source_url) == expected_url


def test_normalize_url_strips_credentials() -> None:
    """Normalized fetching URLs must not contain user credentials."""
    cases = (
        (
            GenericProvider(),
            "https://user:pass@example.com/blueprint.yaml",
            "https://example.com/blueprint.yaml",
        ),
        (
            GitHubProvider(),
            "https://user:pass@github.com/user/repo/blob/main/blueprint.yaml",
            "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml",
        ),
        (
            GistProvider(),
            "https://user:pass@gist.github.com/user/abc123",
            "https://gist.github.com/user/abc123/raw",
        ),
        (
            GitLabProvider(),
            "https://user:pass@gitlab.com/user/repo/-/blob/main/bp.yaml",
            "https://gitlab.com/user/repo/-/raw/main/bp.yaml",
        ),
        (
            CodebergProvider(),
            "https://user:pass@codeberg.org/user/repo/src/branch/main/bp.yaml",
            "https://codeberg.org/user/repo/raw/branch/main/bp.yaml",
        ),
        (
            BitbucketProvider(),
            "https://user:pass@bitbucket.org/user/repo/src/master/bp.yaml",
            "https://bitbucket.org/user/repo/raw/master/bp.yaml",
        ),
        (
            HAForumProvider(),
            "https://user:pass@community.home-assistant.io/t/topic/123",
            "https://community.home-assistant.io/t/123.json",
        ),
    )

    for provider, source_url, expected_url in cases:
        assert provider.normalize_url(source_url) == expected_url


def test_registry_normalize_url_removes_fragments() -> None:
    """Registry normalization must remove fragments for every provider."""
    reg = ProviderRegistry()
    cases = (
        (
            "https://example.com/blueprint.yaml#section",
            "https://example.com/blueprint.yaml",
        ),
        (
            "https://github.com/user/repo/blob/main/blueprint.yaml#section",
            "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml",
        ),
        (
            "https://community.home-assistant.io/t/topic/123#section",
            "https://community.home-assistant.io/t/123.json",
        ),
    )

    for source_url, expected_url in cases:
        assert reg.normalize_url(source_url) == expected_url


def test_canonicalization_ignores_generic_url_fragments() -> None:
    """Generic URLs differing only by fragments must have the same identity."""
    provider = GenericProvider()
    base = "https://example.com/blueprint.yaml"

    assert provider.canonicalize_url(base) == provider.canonicalize_url(f"{base}#section")
    assert provider.is_same_source(base, f"{base}#section") is True


def test_canonicalization_ignores_github_url_fragments() -> None:
    """GitHub URLs differing only by fragments must have the same identity."""
    provider = GitHubProvider()
    base = "https://github.com/user/repo/blob/main/blueprint.yaml"

    assert provider.canonicalize_url(base) == provider.canonicalize_url(f"{base}#section")
    assert provider.is_same_source(base, f"{base}#section") is True


def test_generic_metadata_fallback_ignores_url_fragment() -> None:
    """Generic fallback names must ignore URL fragments."""
    provider = GenericProvider()
    base = "https://example.com/source"

    assert provider.get_metadata(base) == provider.get_metadata(f"{base}#section")


def test_generic_provider_preserves_explicit_port() -> None:
    """Generic URL canonicalization must preserve an explicit source port."""
    provider = GenericProvider()
    url = "https://example.com:8443/blueprint.yaml"

    assert provider.canonicalize_url(url) == url


def test_dedicated_provider_strips_port_on_already_normalized_git_url() -> None:
    """Dedicated Git providers must strip default ports even when no path rewrite is needed."""
    cases = (
        (
            GitLabProvider(),
            "https://gitlab.com:443/user/repo/-/raw/main/bp.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:443/user/repo/raw/branch/main/bp.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:443/user/repo/raw/master/bp.yaml",
        ),
    )

    for provider, url in cases:
        assert ":443" not in provider.normalize_url(url)


def test_generic_provider_preserves_ipv6_port_zero() -> None:
    """Generic canonicalization must preserve an explicit zero port."""
    provider = GenericProvider()
    url = "https://[::1]:0/blueprint.yaml"

    assert provider.canonicalize_url(url) == url


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://example.com:invalid/blueprint.yaml",
        "https://example.com:70000/blueprint.yaml",
    ],
)
def test_generic_provider_handles_malformed_port_gracefully(malformed_url: str) -> None:
    """Generic canonicalization must handle malformed or out-of-range ports safely.

    Malformed ports are preserved verbatim so the URL retains a distinct canonical
    identity — it must not be silently collapsed onto the port-free form of the same host.
    """
    provider = GenericProvider()
    canonical = provider.canonicalize_url(malformed_url)
    # The broken port is kept; the result is NOT the same as the port-free URL.
    assert canonical != "https://example.com/blueprint.yaml"
    assert urlparse(canonical).hostname == "example.com"
    # are_same_source must treat the malformed-port URL as a distinct source.
    reg = ProviderRegistry()
    assert reg.are_same_source(malformed_url, "https://example.com/blueprint.yaml") is False


@pytest.mark.parametrize(
    ("url_with_port", "url_without_port", "scheme"),
    [
        (
            "https://example.com:443/blueprint.yaml",
            "https://example.com/blueprint.yaml",
            "https",
        ),
        (
            "http://example.com:80/blueprint.yaml",
            "http://example.com/blueprint.yaml",
            "http",
        ),
        (
            "https://github.com:443/user/repo/blob/main/test.yaml",
            "https://github.com/user/repo/blob/main/test.yaml",
            "https",
        ),
        (
            "https://raw.githubusercontent.com:443/user/repo/main/test.yaml",
            "https://raw.githubusercontent.com/user/repo/main/test.yaml",
            "https",
        ),
        (
            "https://gist.github.com:443/user/gist123",
            "https://gist.github.com/user/gist123",
            "https",
        ),
        (
            "https://community.home-assistant.io:443/t/slug/787779",
            "https://community.home-assistant.io/t/slug/787779",
            "https",
        ),
        (
            "https://gitlab.com:443/user/repo/-/blob/main/bp.yaml",
            "https://gitlab.com/user/repo/-/blob/main/bp.yaml",
            "https",
        ),
        (
            "https://codeberg.org:443/user/repo/src/branch/main/bp.yaml",
            "https://codeberg.org/user/repo/src/branch/main/bp.yaml",
            "https",
        ),
        (
            "https://bitbucket.org:443/user/repo/src/master/bp.yaml",
            "https://bitbucket.org/user/repo/src/master/bp.yaml",
            "https",
        ),
    ],
)
def test_registry_are_same_source_treats_default_port_as_equivalent(
    url_with_port: str, url_without_port: str, scheme: str
) -> None:
    """registry.are_same_source must treat explicit default ports as equivalent.

    https://example.com:443/… and https://example.com/… are the same source;
    http://example.com:80/… and http://example.com/… are likewise the same source.
    """
    reg = ProviderRegistry()
    assert reg.are_same_source(url_with_port, url_without_port) is True
    assert reg.are_same_source(url_without_port, url_with_port) is True
    # The canonical form must not contain the default port.
    assert f":{443 if scheme == 'https' else 80}" not in reg.canonicalize_url(url_with_port)


def test_generic_provider_preserves_non_default_port_in_canonical_url() -> None:
    """GenericProvider must preserve non-default ports such as 8443 in canonical form."""
    provider = GenericProvider()
    url = "https://example.com:8443/blueprint.yaml"

    canonical = provider.canonicalize_url(url)
    assert ":8443" in canonical
    assert canonical == url


@pytest.mark.parametrize(
    ("provider", "url_with_default_port", "expected_canonical"),
    [
        (
            GitHubProvider(),
            "https://github.com:443/user/repo/blob/main/blueprint.yaml",
            "https://github.com/user/repo/blob/main/blueprint.yaml",
        ),
        (
            GitHubProvider(),
            "https://raw.githubusercontent.com:443/user/repo/main/blueprint.yaml",
            "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml",
        ),
        (
            GistProvider(),
            "https://gist.github.com:443/user/gist_id",
            "https://gist.github.com/user/gist_id",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:443/t/title-slug/12345",
            "https://community.home-assistant.io/t/12345",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com:443/user/repo/-/blob/main/blueprint.yaml",
            "https://gitlab.com/user/repo/-/blob/main/blueprint.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:443/user/repo/src/branch/main/blueprint.yaml",
            "https://codeberg.org/user/repo/src/branch/main/blueprint.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:443/user/repo/src/master/blueprint.yaml",
            "https://bitbucket.org/user/repo/src/master/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:443/blueprint.yaml",
            "https://example.com/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "http://example.com:80/blueprint.yaml",
            "http://example.com/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://[::1]:443/blueprint.yaml",
            "https://[::1]/blueprint.yaml",
        ),
    ],
)
def test_all_providers_omit_default_ports_in_canonicalize_url(
    provider: SourceProvider, url_with_default_port: str, expected_canonical: str
) -> None:
    """All providers must omit default ports for the scheme in canonicalize_url."""
    assert provider.canonicalize_url(url_with_default_port) == expected_canonical


@pytest.mark.parametrize(
    ("provider", "url_with_non_default_port", "expected_canonical"),
    [
        (
            GitHubProvider(),
            "https://github.com:8443/user/repo/blob/main/blueprint.yaml",
            "https://github.com:8443/user/repo/blob/main/blueprint.yaml",
        ),
        (
            GitHubProvider(),
            "https://github.com:0/user/repo/blob/main/blueprint.yaml",
            "https://github.com:0/user/repo/blob/main/blueprint.yaml",
        ),
        (
            GistProvider(),
            "https://gist.github.com:8443/user/gist_id",
            "https://gist.github.com:8443/user/gist_id",
        ),
        (
            GistProvider(),
            "https://gist.github.com:0/user/gist_id",
            "https://gist.github.com:0/user/gist_id",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:8443/t/title-slug/12345",
            "https://community.home-assistant.io:8443/t/12345",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:0/t/title-slug/12345",
            "https://community.home-assistant.io:0/t/12345",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com:8443/user/repo/-/blob/main/blueprint.yaml",
            "https://gitlab.com:8443/user/repo/-/blob/main/blueprint.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:8443/user/repo/src/branch/main/blueprint.yaml",
            "https://codeberg.org:8443/user/repo/src/branch/main/blueprint.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:8443/user/repo/src/master/blueprint.yaml",
            "https://bitbucket.org:8443/user/repo/src/master/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:8443/blueprint.yaml",
            "https://example.com:8443/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "http://example.com:8080/blueprint.yaml",
            "http://example.com:8080/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:0/blueprint.yaml",
            "https://example.com:0/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "http://example.com:443/blueprint.yaml",
            "http://example.com:443/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:80/blueprint.yaml",
            "https://example.com:80/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://[::1]:0/blueprint.yaml",
            "https://[::1]:0/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://[::1]:8443/blueprint.yaml",
            "https://[::1]:8443/blueprint.yaml",
        ),
    ],
)
def test_all_providers_preserve_non_default_ports_in_canonicalize_url(
    provider: SourceProvider, url_with_non_default_port: str, expected_canonical: str
) -> None:
    """All providers must preserve non-default ports (including 8443 and 0) in canonicalize_url."""
    assert provider.canonicalize_url(url_with_non_default_port) == expected_canonical


@pytest.mark.parametrize(
    ("provider", "url_with_port", "expected_normalized"),
    [
        (
            GitHubProvider(),
            "https://raw.githubusercontent.com:443/user/repo/main/blueprint.yaml",
            "https://raw.githubusercontent.com/user/repo/main/blueprint.yaml",
        ),
        (
            GitHubProvider(),
            "https://raw.githubusercontent.com:8443/user/repo/main/blueprint.yaml",
            "https://raw.githubusercontent.com:8443/user/repo/main/blueprint.yaml",
        ),
        (
            GistProvider(),
            "https://gist.github.com:443/user/gist_id",
            "https://gist.github.com/user/gist_id/raw",
        ),
        (
            GistProvider(),
            "https://gist.github.com:8443/user/gist_id",
            "https://gist.github.com:8443/user/gist_id/raw",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:443/t/title-slug/12345",
            "https://community.home-assistant.io/t/12345.json",
        ),
        (
            HAForumProvider(),
            "https://community.home-assistant.io:8443/t/title-slug/12345",
            "https://community.home-assistant.io:8443/t/12345.json",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com:443/user/repo/-/blob/main/blueprint.yaml",
            "https://gitlab.com/user/repo/-/raw/main/blueprint.yaml",
        ),
        (
            GitLabProvider(),
            "https://gitlab.com:8443/user/repo/-/blob/main/blueprint.yaml",
            "https://gitlab.com:8443/user/repo/-/raw/main/blueprint.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:443/user/repo/src/branch/main/blueprint.yaml",
            "https://codeberg.org/user/repo/raw/branch/main/blueprint.yaml",
        ),
        (
            CodebergProvider(),
            "https://codeberg.org:8443/user/repo/src/branch/main/blueprint.yaml",
            "https://codeberg.org:8443/user/repo/raw/branch/main/blueprint.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:443/user/repo/src/master/blueprint.yaml",
            "https://bitbucket.org/user/repo/raw/master/blueprint.yaml",
        ),
        (
            BitbucketProvider(),
            "https://bitbucket.org:8443/user/repo/src/master/blueprint.yaml",
            "https://bitbucket.org:8443/user/repo/raw/master/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:443/blueprint.yaml#section",
            "https://example.com/blueprint.yaml",
        ),
        (
            GenericProvider(),
            "https://example.com:8443/blueprint.yaml#section",
            "https://example.com:8443/blueprint.yaml",
        ),
    ],
)
def test_all_providers_normalize_url_port_handling(
    provider: SourceProvider, url_with_port: str, expected_normalized: str
) -> None:
    """All providers must omit default ports and preserve non-default ports in normalize_url."""
    assert provider.normalize_url(url_with_port) == expected_normalized


@pytest.mark.parametrize(
    "malformed_ipv6_url",
    [
        "https://[::1/path",
        "https://[2001:db8::1/path",
    ],
)
def test_registry_are_same_source_malformed_ipv6_returns_false(
    malformed_ipv6_url: str,
) -> None:
    """are_same_source must return False for unclosed-bracket IPv6 URLs without raising.

    urlparse raises ValueError('Invalid IPv6 URL') for these inputs; the guard in
    are_same_source catches it and returns False when comparing against other URLs.
    Exact identical strings return True via the string-equality fast path.
    """
    reg = ProviderRegistry()
    valid_url = "https://example.com/blueprint.yaml"
    # Must not raise, must return False — a malformed URL has no shared identity.
    assert reg.are_same_source(malformed_ipv6_url, valid_url) is False
    assert reg.are_same_source(valid_url, malformed_ipv6_url) is False
    # Two identically malformed URLs compare equal only via the fast string path.
    assert reg.are_same_source(malformed_ipv6_url, malformed_ipv6_url) is True


def test_registry_are_same_source_fragment_only_difference_is_equal() -> None:
    """are_same_source must treat fragment-only differences as the same source.

    URL fragments are client-side anchors and do not identify a distinct resource.
    """
    reg = ProviderRegistry()
    base = "https://example.com/blueprint.yaml"
    assert reg.are_same_source(base, f"{base}#section") is True
    assert reg.are_same_source(f"{base}#section1", f"{base}#section2") is True
    assert reg.are_same_source(f"{base}#section", base) is True


def test_registry_are_same_source_distinct_query_params_are_not_equal() -> None:
    """are_same_source must treat distinct query parameters as different sources.

    Query strings identify distinct resources (e.g. ?file=1 and ?file=2 resolve
    to different content), so they must not share an identity.
    """
    reg = ProviderRegistry()
    base = "https://example.com/blueprint.yaml"
    assert reg.are_same_source(f"{base}?file=1", f"{base}?file=2") is False
    assert reg.are_same_source(f"{base}?v=1", base) is False


def test_registry_are_same_source_preserves_query_parameters() -> None:
    """are_same_source must preserve query parameters when comparing equivalent URLs."""
    reg = ProviderRegistry()
    base = "https://example.com/blueprint.yaml?file=1"
    assert reg.are_same_source(base, f"{base}#section") is True
    assert (
        reg.are_same_source(
            "https://example.com:443/blueprint.yaml?file=1",
            "HTTPS://example.com/blueprint.yaml?file=1#section",
        )
        is True
    )


@pytest.mark.parametrize(
    ("url_with_credentials", "expected_canonical"),
    [
        (
            "https://user:pass@example.com/blueprint.yaml",
            "https://example.com/blueprint.yaml",
        ),
        (
            "https://user:pass@example.com:443/blueprint.yaml",
            "https://example.com/blueprint.yaml",
        ),
        (
            "https://user:pass@example.com:8443/blueprint.yaml",
            "https://example.com:8443/blueprint.yaml",
        ),
        (
            "https://user@example.com:443/blueprint.yaml",
            "https://example.com/blueprint.yaml",
        ),
        (
            "https://user:pass@[::1]:8443/blueprint.yaml",
            "https://[::1]:8443/blueprint.yaml",
        ),
        (
            "https://user:pass@[::1]:443/blueprint.yaml",
            "https://[::1]/blueprint.yaml",
        ),
        (
            "HTTPS://user:pass@github.com:443/owner/repo/blob/main/test.yaml",
            "https://github.com/owner/repo/blob/main/test.yaml",
        ),
        (
            "HTTP://EXAMPLE.COM:80/blueprint.yaml",
            "http://example.com/blueprint.yaml",
        ),
    ],
)
def test_canonicalize_url_strips_credentials_and_lowercases_scheme(
    url_with_credentials: str, expected_canonical: str
) -> None:
    """canonicalize_url must strip user credentials and lowercase scheme across URLs."""
    provider = GenericProvider()
    assert provider.canonicalize_url(url_with_credentials) == expected_canonical


def test_registry_are_same_source_strips_credentials_and_omits_default_port() -> None:
    """Test are_same_source treats URLs with credentials and default port as equivalent."""
    reg = ProviderRegistry()
    url1 = "https://user:pass@example.com:443/blueprint.yaml"
    url2 = "HTTPS://other:pass@example.com/blueprint.yaml"
    url3 = "https://example.com/blueprint.yaml"

    assert reg.are_same_source(url1, url2) is True
    assert reg.are_same_source(url1, url3) is True


@pytest.mark.parametrize(
    ("malformed_url", "expected_fallback"),
    [
        ("  https://[::1/path  ", "https://[::1/path"),
        ("  https://[2001:db8::1/path  ", "https://[2001:db8::1/path"),
        (
            "  https://example.com:invalid_port/bp.yaml  ",
            "https://example.com:invalid_port/bp.yaml",
        ),
        ("  https://example.com:99999/bp.yaml  ", "https://example.com:99999/bp.yaml"),
    ],
)
def test_registry_canonicalize_url_malformed_url_fallback(
    malformed_url: str, expected_fallback: str
) -> None:
    """Test ProviderRegistry.canonicalize_url fallback for malformed inputs."""
    reg = ProviderRegistry()
    assert reg.canonicalize_url(malformed_url) == expected_fallback
