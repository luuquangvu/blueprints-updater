"""Tests for blueprint source providers (providers.py)."""

import pytest

from custom_components.blueprints_updater.providers import (
    GistProvider,
    GitHubProvider,
    HAForumProvider,
    ProviderRegistry,
    SourceProvider,
    _normalize_hostname,
    registry,
)


# ---------------------------------------------------------------------------
# _normalize_hostname
# ---------------------------------------------------------------------------


class TestNormalizeHostname:
    """Tests for the _normalize_hostname helper."""

    def test_none_returns_empty_string(self):
        """None input returns empty string."""
        assert _normalize_hostname(None) == ""

    def test_empty_string_returns_empty_string(self):
        """Empty string input returns empty string."""
        assert _normalize_hostname("") == ""

    def test_lowercase_passthrough(self):
        """Already lowercase hostname returns unchanged."""
        assert _normalize_hostname("github.com") == "github.com"

    def test_uppercase_is_lowercased(self):
        """Uppercase hostname is lowercased."""
        assert _normalize_hostname("GitHub.COM") == "github.com"

    def test_www_prefix_stripped(self):
        """www. prefix is stripped."""
        assert _normalize_hostname("www.github.com") == "github.com"

    def test_www_uppercase_stripped(self):
        """WWW. prefix (uppercase) is also stripped after lowercasing."""
        assert _normalize_hostname("WWW.GitHub.COM") == "github.com"

    def test_non_www_subdomain_preserved(self):
        """Non-www subdomain is preserved."""
        assert _normalize_hostname("raw.githubusercontent.com") == "raw.githubusercontent.com"

    def test_www_only_returns_empty_after_strip(self):
        """'www.' alone returns empty string after stripping."""
        assert _normalize_hostname("www.") == ""


# ---------------------------------------------------------------------------
# GitHubProvider
# ---------------------------------------------------------------------------


class TestGitHubProvider:
    """Tests for GitHubProvider."""

    def setup_method(self):
        """Set up provider instance."""
        self.provider = GitHubProvider()

    # --- can_handle ---

    def test_can_handle_github_com(self):
        """Handles standard github.com URLs."""
        assert self.provider.can_handle("https://github.com/user/repo/blob/main/file.yaml")

    def test_can_handle_raw_githubusercontent(self):
        """Handles raw.githubusercontent.com URLs."""
        assert self.provider.can_handle(
            "https://raw.githubusercontent.com/user/repo/main/file.yaml"
        )

    def test_can_handle_www_github_com(self):
        """Handles www.github.com URLs (www. is stripped)."""
        assert self.provider.can_handle("https://www.github.com/user/repo/blob/main/file.yaml")

    def test_cannot_handle_gist(self):
        """Does not handle Gist URLs."""
        assert not self.provider.can_handle("https://gist.github.com/user/abc123")

    def test_cannot_handle_forum(self):
        """Does not handle Forum URLs."""
        assert not self.provider.can_handle("https://community.home-assistant.io/t/topic/123")

    def test_cannot_handle_arbitrary_url(self):
        """Does not handle arbitrary URLs."""
        assert not self.provider.can_handle("https://example.com/file.yaml")

    # --- normalize_url ---

    def test_normalize_github_blob_url(self):
        """Converts github.com blob URL to raw.githubusercontent.com URL."""
        url = "https://github.com/user/repo/blob/main/automations/file.yaml"
        result = self.provider.normalize_url(url)
        assert result == "https://raw.githubusercontent.com/user/repo/main/automations/file.yaml"

    def test_normalize_raw_url_unchanged(self):
        """Raw URL is returned unchanged."""
        url = "https://raw.githubusercontent.com/user/repo/main/file.yaml"
        assert self.provider.normalize_url(url) == url

    def test_normalize_github_url_without_blob_unchanged(self):
        """github.com URL without /blob/ is returned unchanged."""
        url = "https://github.com/user/repo"
        assert self.provider.normalize_url(url) == url

    def test_normalize_github_url_too_short_unchanged(self):
        """github.com URL with fewer than 5 path parts is returned unchanged."""
        url = "https://github.com/user/repo/blob/main"
        assert self.provider.normalize_url(url) == url

    def test_normalize_url_preserves_query_and_fragment(self):
        """Query and fragment are preserved during normalization."""
        url = "https://github.com/user/repo/blob/main/file.yaml?foo=bar#section"
        result = self.provider.normalize_url(url)
        assert "raw.githubusercontent.com" in result
        assert "foo=bar" in result
        assert "section" in result

    # --- get_cdn_url ---

    def test_get_cdn_url_from_raw_url(self):
        """Generates jsDelivr CDN URL from raw.githubusercontent.com URL."""
        url = "https://raw.githubusercontent.com/user/repo/main/automations/file.yaml"
        result = self.provider.get_cdn_url(url)
        assert result == "https://cdn.jsdelivr.net/gh/user/repo@main/automations/file.yaml"

    def test_get_cdn_url_from_github_blob_url(self):
        """Generates jsDelivr CDN URL from github.com blob URL."""
        url = "https://github.com/user/repo/blob/main/file.yaml"
        result = self.provider.get_cdn_url(url)
        assert result == "https://cdn.jsdelivr.net/gh/user/repo@main/file.yaml"

    def test_get_cdn_url_from_github_raw_url(self):
        """Generates jsDelivr CDN URL from github.com raw URL."""
        url = "https://github.com/user/repo/raw/main/file.yaml"
        result = self.provider.get_cdn_url(url)
        assert result == "https://cdn.jsdelivr.net/gh/user/repo@main/file.yaml"

    def test_get_cdn_url_raw_too_short_returns_none(self):
        """Returns None when raw URL has fewer than 4 path parts."""
        url = "https://raw.githubusercontent.com/user/repo/main"
        assert self.provider.get_cdn_url(url) == None

    def test_get_cdn_url_github_too_short_returns_none(self):
        """Returns None when github.com URL has fewer than 5 path parts."""
        url = "https://github.com/user/repo/blob"
        assert self.provider.get_cdn_url(url) == None

    def test_get_cdn_url_github_no_blob_or_raw_returns_none(self):
        """Returns None when github.com URL doesn't contain blob or raw anchor."""
        url = "https://github.com/user/repo/tree/main/file.yaml"
        assert self.provider.get_cdn_url(url) == None

    def test_get_cdn_url_unhandled_hostname_returns_none(self):
        """Returns None for unhandled hostnames."""
        url = "https://example.com/user/repo/blob/main/file.yaml"
        assert self.provider.get_cdn_url(url) == None

    # --- parse_content (inherited default) ---

    def test_parse_content_returns_text(self):
        """Default parse_content returns response_text."""
        assert self.provider.parse_content("blueprint:\n  name: test") == "blueprint:\n  name: test"


# ---------------------------------------------------------------------------
# GistProvider
# ---------------------------------------------------------------------------


class TestGistProvider:
    """Tests for GistProvider."""

    def setup_method(self):
        """Set up provider instance."""
        self.provider = GistProvider()

    # --- can_handle ---

    def test_can_handle_gist_url(self):
        """Handles gist.github.com URLs."""
        assert self.provider.can_handle("https://gist.github.com/user/abc123")

    def test_cannot_handle_github_url(self):
        """Does not handle github.com URLs."""
        assert not self.provider.can_handle("https://github.com/user/repo")

    def test_cannot_handle_forum_url(self):
        """Does not handle forum URLs."""
        assert not self.provider.can_handle("https://community.home-assistant.io/t/topic/123")

    # --- normalize_url ---

    def test_normalize_gist_url_adds_raw(self):
        """Appends /raw to gist URL without /raw."""
        url = "https://gist.github.com/user/abc123def456"
        result = self.provider.normalize_url(url)
        assert result == "https://gist.github.com/user/abc123def456/raw"

    def test_normalize_gist_url_already_raw_unchanged(self):
        """URL already ending in /raw is returned unchanged."""
        url = "https://gist.github.com/user/abc123def456/raw"
        result = self.provider.normalize_url(url)
        assert result == url

    def test_normalize_gist_url_raw_with_trailing_slash(self):
        """URL with /raw/ (trailing slash) is returned unchanged."""
        url = "https://gist.github.com/user/abc123def456/raw/"
        result = self.provider.normalize_url(url)
        assert result == url

    def test_normalize_gist_preserves_query_params(self):
        """Query parameters are preserved during normalization."""
        url = "https://gist.github.com/user/abc123?ts=4"
        result = self.provider.normalize_url(url)
        assert result.endswith("/raw")
        assert "ts=4" in result

    # --- get_cdn_url (default: None) ---

    def test_get_cdn_url_returns_none(self):
        """GistProvider does not provide a CDN URL."""
        assert self.provider.get_cdn_url("https://gist.github.com/user/abc123/raw") is None

    # --- parse_content (default) ---

    def test_parse_content_returns_text(self):
        """Default parse_content returns response_text."""
        text = "blueprint:\n  name: test"
        assert self.provider.parse_content(text) == text


# ---------------------------------------------------------------------------
# HAForumProvider
# ---------------------------------------------------------------------------


class TestHAForumProvider:
    """Tests for HAForumProvider."""

    def setup_method(self):
        """Set up provider instance."""
        self.provider = HAForumProvider()

    def _make_forum_json(self, cooked: str) -> dict:
        """Helper to build a forum JSON payload with given cooked HTML."""
        return {
            "post_stream": {
                "posts": [
                    {"cooked": cooked},
                ]
            }
        }

    # --- can_handle ---

    def test_can_handle_forum_url(self):
        """Handles community.home-assistant.io URLs."""
        assert self.provider.can_handle("https://community.home-assistant.io/t/topic/123")

    def test_cannot_handle_github_url(self):
        """Does not handle github.com URLs."""
        assert not self.provider.can_handle("https://github.com/user/repo")

    def test_cannot_handle_gist_url(self):
        """Does not handle Gist URLs."""
        assert not self.provider.can_handle("https://gist.github.com/user/abc123")

    # --- normalize_url ---

    def test_normalize_forum_url_with_slug_and_id(self):
        """Converts forum URL with slug to JSON endpoint."""
        url = "https://community.home-assistant.io/t/my-blueprint/12345"
        result = self.provider.normalize_url(url)
        assert result == "https://community.home-assistant.io/t/12345.json"

    def test_normalize_forum_url_with_id_only(self):
        """Converts forum URL with only numeric ID."""
        url = "https://community.home-assistant.io/t/12345"
        result = self.provider.normalize_url(url)
        assert result == "https://community.home-assistant.io/t/12345.json"

    def test_normalize_forum_url_without_topic_unchanged(self):
        """URL without /t/ is returned unchanged."""
        url = "https://community.home-assistant.io/category/blueprints"
        assert self.provider.normalize_url(url) == url

    def test_normalize_forum_url_no_id_unchanged(self):
        """URL with /t/ but no numeric ID is returned unchanged."""
        url = "https://community.home-assistant.io/t/no-id-here/"
        assert self.provider.normalize_url(url) == url

    def test_normalize_forum_url_strips_query_string(self):
        """Query string is stripped in normalized URL."""
        url = "https://community.home-assistant.io/t/my-bp/123?page=2"
        result = self.provider.normalize_url(url)
        assert "page=2" not in result
        assert result.endswith("123.json")

    # --- parse_content ---

    def test_parse_content_extracts_blueprint_from_code_block(self):
        """Extracts blueprint YAML from a code block in forum post."""
        cooked = "<code>blueprint:\n  name: Test BP\n</code>"
        result = self.provider.parse_content("", self._make_forum_json(cooked))
        assert result is not None
        assert "blueprint:" in result

    def test_parse_content_returns_none_for_non_dict_json(self):
        """Returns None when response_json is not a dict."""
        assert self.provider.parse_content("text", None) is None
        assert self.provider.parse_content("text", []) is None  # type: ignore[arg-type]

    def test_parse_content_returns_none_for_missing_post_stream(self):
        """Returns None when post_stream key is missing."""
        assert self.provider.parse_content("", {"other": "data"}) is None

    def test_parse_content_returns_none_for_non_dict_post_stream(self):
        """Returns None when post_stream is not a dict."""
        assert self.provider.parse_content("", {"post_stream": "bad"}) is None

    def test_parse_content_returns_none_for_missing_posts(self):
        """Returns None when posts key is missing."""
        assert self.provider.parse_content("", {"post_stream": {}}) is None

    def test_parse_content_returns_none_for_non_list_posts(self):
        """Returns None when posts is not a list."""
        assert self.provider.parse_content("", {"post_stream": {"posts": "bad"}}) is None

    def test_parse_content_returns_none_when_no_blueprint_in_code_block(self):
        """Returns None when code blocks exist but none contain 'blueprint:'."""
        cooked = "<code>not_a_blueprint: true</code>"
        assert self.provider.parse_content("", self._make_forum_json(cooked)) is None

    def test_parse_content_skips_non_dict_posts(self):
        """Skips non-dict entries in posts list."""
        json_data = {
            "post_stream": {
                "posts": [
                    "not a dict",
                    {"cooked": "<code>blueprint:\n  name: Valid\n</code>"},
                ]
            }
        }
        result = self.provider.parse_content("", json_data)
        assert result is not None
        assert "blueprint:" in result

    def test_parse_content_skips_non_string_cooked(self):
        """Skips posts where 'cooked' is not a string."""
        json_data = {
            "post_stream": {
                "posts": [
                    {"cooked": 42},
                    {"cooked": "<code>blueprint:\n  name: Valid\n</code>"},
                ]
            }
        }
        result = self.provider.parse_content("", json_data)
        assert result is not None
        assert "blueprint:" in result

    def test_parse_content_unescapes_html_entities(self):
        """HTML entities in code blocks are unescaped."""
        cooked = "<code>blueprint:\n  name: &lt;Test&gt;\n  description: &amp; more\n</code>"
        result = self.provider.parse_content("", self._make_forum_json(cooked))
        assert result is not None
        assert "<Test>" in result
        assert "& more" in result

    def test_parse_content_finds_blueprint_in_second_code_block(self):
        """Finds blueprint in second code block if first has none."""
        cooked = "<code>not a blueprint</code><code>blueprint:\n  name: Found\n</code>"
        result = self.provider.parse_content("", self._make_forum_json(cooked))
        assert result is not None
        assert "blueprint:" in result

    def test_parse_content_searches_all_posts(self):
        """Searches multiple posts to find blueprint."""
        json_data = {
            "post_stream": {
                "posts": [
                    {"cooked": "<code>no blueprint here</code>"},
                    {"cooked": "<code>blueprint:\n  name: InSecondPost\n</code>"},
                ]
            }
        }
        result = self.provider.parse_content("", json_data)
        assert result is not None
        assert "InSecondPost" in result


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    """Tests for ProviderRegistry."""

    def setup_method(self):
        """Set up fresh registry instance."""
        self.registry = ProviderRegistry()

    def test_get_provider_for_github_url(self):
        """Returns GitHubProvider for github.com URL."""
        provider = self.registry.get_provider("https://github.com/user/repo/blob/main/file.yaml")
        assert isinstance(provider, GitHubProvider)

    def test_get_provider_for_raw_github_url(self):
        """Returns GitHubProvider for raw.githubusercontent.com URL."""
        provider = self.registry.get_provider(
            "https://raw.githubusercontent.com/user/repo/main/file.yaml"
        )
        assert isinstance(provider, GitHubProvider)

    def test_get_provider_for_gist_url(self):
        """Returns GistProvider for gist.github.com URL."""
        provider = self.registry.get_provider("https://gist.github.com/user/abc123")
        assert isinstance(provider, GistProvider)

    def test_get_provider_for_forum_url(self):
        """Returns HAForumProvider for community.home-assistant.io URL."""
        provider = self.registry.get_provider("https://community.home-assistant.io/t/topic/123")
        assert isinstance(provider, HAForumProvider)

    def test_get_provider_for_unknown_url_returns_none(self):
        """Returns None for URLs not handled by any provider."""
        provider = self.registry.get_provider("https://example.com/file.yaml")
        assert provider is None

    def test_iter_yields_all_providers(self):
        """Iterating over registry yields all three default providers."""
        providers = list(self.registry)
        assert len(providers) == 3
        provider_types = {type(p) for p in providers}
        assert GitHubProvider in provider_types
        assert GistProvider in provider_types
        assert HAForumProvider in provider_types

    def test_all_providers_are_source_provider_subclasses(self):
        """All registered providers are SourceProvider subclasses."""
        for provider in self.registry:
            assert isinstance(provider, SourceProvider)

    # --- module-level registry singleton ---

    def test_module_registry_is_provider_registry_instance(self):
        """The module-level registry is a ProviderRegistry instance."""
        assert isinstance(registry, ProviderRegistry)

    def test_module_registry_has_correct_providers(self):
        """Module-level registry resolves to correct providers."""
        assert isinstance(
            registry.get_provider("https://github.com/user/repo/blob/main/file.yaml"),
            GitHubProvider,
        )
        assert isinstance(
            registry.get_provider("https://gist.github.com/user/abc123"),
            GistProvider,
        )
        assert isinstance(
            registry.get_provider("https://community.home-assistant.io/t/topic/123"),
            HAForumProvider,
        )

    def test_get_provider_returns_none_for_empty_url(self):
        """Returns None for empty string URL."""
        assert self.registry.get_provider("") is None

    def test_get_provider_priority_github_before_gist(self):
        """GitHub provider is registered before Gist (priority order)."""
        providers = list(self.registry)
        github_idx = next(i for i, p in enumerate(providers) if isinstance(p, GitHubProvider))
        gist_idx = next(i for i, p in enumerate(providers) if isinstance(p, GistProvider))
        assert github_idx < gist_idx