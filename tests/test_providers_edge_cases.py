"""Edge case tests for Source Providers.

Includes hostname normalization and malformed response parsing.
"""

from custom_components.blueprints_updater.providers import (
    GitHubProvider,
    HAForumProvider,
    _normalize_hostname,
    registry,
)


def test_normalize_hostname_edge_cases():
    """Test _normalize_hostname with edge cases."""
    assert _normalize_hostname(None) == ""
    assert _normalize_hostname("WWW.GITHUB.COM") == "github.com"
    assert _normalize_hostname("github.com") == "github.com"


def test_github_provider_cdn_url_edge_cases():
    """Test GitHubProvider.get_cdn_url with malformed or unsupported URLs."""
    provider = GitHubProvider()

    assert provider.get_cdn_url("https://github.com/user/repo/not-blob/branch/file.yaml") is None

    assert provider.get_cdn_url("https://raw.githubusercontent.com/user/repo/branch") is None

    assert provider.get_cdn_url("https://github.com/user/repo/blob") is None

    assert provider.get_cdn_url("https://example.com/user/repo/blob/branch/file.yaml") is None


def test_ha_forum_provider_normalize_url_edge_cases():
    """Test HAForumProvider.normalize_url with non-topic URLs."""
    provider = HAForumProvider()

    assert (
        provider.normalize_url("https://community.home-assistant.io/latest")
        == "https://community.home-assistant.io/latest"
    )
    assert (
        provider.normalize_url("https://community.home-assistant.io/t/no-id-here")
        == "https://community.home-assistant.io/t/no-id-here"
    )


def test_ha_forum_provider_parse_content_edge_cases():
    """Test HAForumProvider.parse_content with various malformed payloads."""
    provider = HAForumProvider()

    assert provider.parse_content("some text", None) is None

    assert provider.parse_content("", {"other": "data"}) is None

    assert provider.parse_content("", {"post_stream": {"posts": []}}) is None

    assert provider.parse_content("", {"post_stream": {"posts": [{"cooked": 123}]}}) is None

    assert (
        provider.parse_content(
            "", {"post_stream": {"posts": [{"cooked": "<p>No blueprint here</p>"}]}}
        )
        is None
    )


def test_registry_iteration():
    """Test iterating through the registry."""
    providers = list(registry)
    assert len(providers) >= 3
    assert any(isinstance(p, GitHubProvider) for p in providers)
    assert any(isinstance(p, HAForumProvider) for p in providers)
