"""Tests for blueprint provider metadata extraction and URL normalization."""

import pytest

from custom_components.blueprints_updater.providers import (
    GenericProvider,
    GitHubProvider,
    HAForumProvider,
    ProviderRegistry,
)


@pytest.mark.parametrize(
    ("url", "expected_normalized", "expected_author", "expected_name"),
    [
        (
            "https://github.com/user/repo/blob/main/blueprints/test.yaml",
            "https://raw.githubusercontent.com/user/repo/main/blueprints/test.yaml",
            "user",
            "test",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/master/test.yml",
            "https://raw.githubusercontent.com/user/repo/master/test.yml",
            "user",
            "test",
        ),
        (
            "https://gist.github.com/user/gist_id",
            "https://gist.github.com/user/gist_id/raw",
            "user",
            "gist_id",
        ),
        (
            "https://gitlab.com/user/repo/-/blob/main/bp.yaml",
            "https://gitlab.com/user/repo/-/raw/main/bp.yaml",
            "gitlab.com",
            "bp",
        ),
        (
            "https://bitbucket.org/user/repo/src/master/bp.yaml",
            "https://bitbucket.org/user/repo/raw/master/bp.yaml",
            "bitbucket.org",
            "bp",
        ),
        (
            "https://codeberg.org/user/repo/src/branch/main/bp.yaml",
            "https://codeberg.org/user/repo/raw/branch/main/bp.yaml",
            "codeberg.org",
            "bp",
        ),
        (
            "https://community.home-assistant.io/t/topic-title/123",
            "https://community.home-assistant.io/t/123.json",
            "community.home-assistant.io",
            "123",
        ),
        (
            "https://example.com/blueprints/my_cool_blueprint.yaml",
            "https://example.com/blueprints/my_cool_blueprint.yaml",
            "example.com",
            "my_cool_blueprint",
        ),
    ],
)
def test_provider_extraction(url, expected_normalized, expected_author, expected_name):
    """Test that providers correctly normalize URLs and extract metadata."""
    registry = ProviderRegistry()
    provider = registry.get_provider(url)
    assert provider is not None

    normalized = provider.normalize_url(url)
    assert normalized == expected_normalized

    metadata = provider.get_metadata(url)
    assert metadata["author"] == expected_author
    assert metadata["name"] == expected_name


def test_generic_provider_fallback_name():
    """Test GenericProvider fallback name logic."""
    url = "https://example.com/no_extension"
    provider = GenericProvider()
    metadata = provider.get_metadata(url)
    assert metadata["name"].startswith("blueprint_")
    assert len(metadata["name"]) > 15


def test_generic_provider_with_content():
    """Test GenericProvider name extraction from content."""
    url = "https://example.com/blueprint"
    provider = GenericProvider()

    content = "blueprint:\n  name: Custom Name\n"
    metadata = provider.get_metadata(url, content=content)
    assert metadata["name"] == "custom_name"

    metadata = provider.get_metadata(url, content="invalid: yaml: :")
    assert metadata["name"].startswith("blueprint_")

    metadata = provider.get_metadata(url, content="- item1\n- item2")
    assert metadata["name"].startswith("blueprint_")


def test_ha_forum_provider_invalid_url():
    """Test HA Forum provider with invalid URLs."""
    provider = HAForumProvider()
    url = "https://community.home-assistant.io/not-a-topic"
    assert provider.normalize_url(url) == url
    metadata = provider.get_metadata(url)
    assert metadata["name"] == "topic"


def test_github_provider_unsupported_urls():
    """Test GitHub provider with unsupported URL formats."""
    provider = GitHubProvider()
    url = "https://github.com/user/repo"
    assert provider.normalize_url(url) == url


def test_provider_registry_iter():
    """Test ProviderRegistry iteration."""
    registry = ProviderRegistry()
    providers = list(registry)
    assert len(providers) >= 7
    assert any(isinstance(p, GitHubProvider) for p in providers)
    assert any(isinstance(p, GenericProvider) for p in providers)


def test_provider_registry_get_generic_fallback():
    """Test ProviderRegistry returning GenericProvider for non-standard schemes."""
    registry = ProviderRegistry()
    assert registry.get_provider("invalid_url") is None
    provider = registry.get_provider("ftp://example.com/bp.yaml")
    assert isinstance(provider, GenericProvider)
