"""Tests for issue where self.data is None during startup."""

from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def mock_coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator with minimal mocking."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.data = cast(Any, None)
        return coord


@pytest.mark.asyncio
async def test_async_prune_stale_metadata_none_data(hass, mock_coordinator):
    """Test that _async_prune_stale_metadata does not crash when self.data is None."""
    coordinator = mock_coordinator

    coordinator._persisted_etags = {"stale_path": "etag"}
    coordinator._persisted_hashes = {"stale_path": "hash"}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock),
    ):
        await coordinator._async_prune_stale_metadata(set())

    assert coordinator.data is None
    assert not coordinator._persisted_etags
    assert not coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_git_diff_none_data(hass, mock_coordinator):
    """Test that git diff methods do not crash when self.data is None."""
    coordinator = mock_coordinator

    assert await coordinator.async_get_git_diff("any_path") is None
    assert coordinator.get_cached_git_diff("any_path", "lh", "rh") is None

    coordinator.set_cached_git_diff("any_path", "lh", "rh", "diff")

    assert coordinator.data is None
