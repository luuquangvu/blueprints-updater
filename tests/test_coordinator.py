import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import (
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__", return_value=None
    ):
        return BlueprintUpdateCoordinator(
            hass,
            entry,
            timedelta(hours=24),
            filter_mode=FILTER_MODE_ALL,
        )


def test_normalize_url(coordinator):
    """Test URL normalization."""
    # GitHub blob to raw
    assert (
        coordinator._normalize_url("https://github.com/user/repo/blob/main/blueprints/test.yaml")
        == "https://raw.githubusercontent.com/user/repo/main/blueprints/test.yaml"
    )

    # Gist to raw
    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id")
        == "https://gist.github.com/user/gist_id/raw"
    )

    # Gist already raw
    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id/raw")
        == "https://gist.github.com/user/gist_id/raw"
    )

    # HA Forum topic to JSON API
    assert (
        coordinator._normalize_url("https://community.home-assistant.io/t/topic-slug/12345")
        == "https://community.home-assistant.io/t/12345.json"
    )

    # Other URL unchanged
    assert (
        coordinator._normalize_url("https://example.com/blueprint.yaml")
        == "https://example.com/blueprint.yaml"
    )


def test_parse_forum_content(coordinator):
    """Test parsing forum content."""
    # Valid forum JSON
    json_data = {
        "post_stream": {
            "posts": [
                {
                    "cooked": (
                        '<p>Here is my blueprint:</p><pre><code class="lang-yaml">blueprint:\n'
                        "  name: Test\n  source_url: https://url.com</code></pre>"
                    )
                }
            ]
        }
    }
    content = coordinator._parse_forum_content(json_data)
    assert "blueprint:" in content
    assert "name: Test" in content

    # No blueprint in code block
    json_data_no_bp = {"post_stream": {"posts": [{"cooked": "<code>not a blueprint</code>"}]}}
    assert coordinator._parse_forum_content(json_data_no_bp) is None

    # Empty/Missing posts
    assert coordinator._parse_forum_content({}) is None
    assert coordinator._parse_forum_content({"post_stream": {"posts": []}}) is None


def test_ensure_source_url(coordinator):
    """Test ensuring source_url is present."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    # Missing source_url
    content = "blueprint:\n  name: Test"
    new_content = coordinator._ensure_source_url(content, source_url)
    assert f"source_url: {source_url}" in new_content

    # Already present
    content_with_url = f"blueprint:\n  name: Test\n  source_url: {source_url}"
    assert coordinator._ensure_source_url(content_with_url, source_url) == content_with_url

    # Present with quotes
    content_with_quotes = f"blueprint:\n  name: Test\n  source_url: '{source_url}'"
    assert coordinator._ensure_source_url(content_with_quotes, source_url) == content_with_quotes


def test_scan_blueprints(hass, coordinator):
    """Test scanning blueprints directory."""
    bp_path = "/config/blueprints"
    mock_files = [(bp_path, [], ["valid.yaml", "invalid.yaml", "no_url.yaml", "not_yaml.txt"])]

    valid_content = "blueprint:\n  name: Valid\n  source_url: https://url.com"
    invalid_content = "not: a blueprint"
    no_url_content = "blueprint:\n  name: No URL"

    def open_side_effect(path, encoding=None):
        path_str = str(path)
        basename = os.path.basename(path_str)
        content = ""
        if basename == "valid.yaml":
            content = valid_content
        elif basename == "invalid.yaml":
            content = invalid_content
        elif basename == "no_url.yaml":
            content = no_url_content

        m = MagicMock()
        m.read.return_value = content
        m.__enter__.return_value = m
        return m

    with (
        patch("os.path.isdir", return_value=True),
        patch("os.walk", return_value=mock_files),
        patch("builtins.open", side_effect=open_side_effect),
    ):
        # ALL mode
        results = coordinator._scan_blueprints(hass, FILTER_MODE_ALL, [])
        assert len(results) == 1, f"Expected 1, got {len(results)}: {results.keys()}"
        assert any("valid.yaml" in k for k in results)
        full_path = next(iter(results.keys()))
        assert results[full_path]["rel_path"] == "valid.yaml"

        # WHITELIST mode - including valid.yaml
        results = coordinator._scan_blueprints(hass, FILTER_MODE_WHITELIST, ["valid.yaml"])
        assert len(results) == 1

        # WHITELIST mode - excluding valid.yaml
        results = coordinator._scan_blueprints(hass, FILTER_MODE_WHITELIST, ["other.yaml"])
        assert len(results) == 0

        # BLACKLIST mode - excluding valid.yaml
        results = coordinator._scan_blueprints(hass, FILTER_MODE_BLACKLIST, ["valid.yaml"])
        assert len(results) == 0


@pytest.mark.asyncio
async def test_async_update_blueprint(coordinator):
    """Test the full update flow for a single blueprint."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "hash": "old_hash",
    }
    results = {}

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.text = AsyncMock(return_value="blueprint:\n  name: Test")

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response

    semaphore = asyncio.Semaphore(1)

    with patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash:
        # Mock hash to be different
        mock_hash.return_value.hexdigest.return_value = "new_hash"

        await coordinator._async_update_blueprint(mock_session, semaphore, path, info, results)

    assert path in results
    assert results[path]["updatable"] is True
    assert results[path]["remote_hash"] == "new_hash"
    assert "source_url" in results[path]["remote_content"]
