"""Tests for the coordinator's data initialization contract and robustness."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def mock_coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator with minimal mocking."""

    def mock_init(self, hass, logger, name, update_interval=None) -> None:
        """Mock side effect to simulate base class setup without failing checks."""
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        autospec=True,
        side_effect=mock_init,
    ):
        return BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))


@pytest.mark.asyncio
async def test_async_prune_stale_metadata_empty_data(hass, mock_coordinator):
    """Test that _async_prune_stale_metadata works correctly with empty self.data."""
    coordinator = mock_coordinator
    coordinator.data = {}

    coordinator._persisted_etags = {"stale_path": "etag"}
    coordinator._persisted_hashes = {"stale_path": "hash"}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock) as mock_save,
    ):
        await coordinator._async_prune_stale_metadata(set())
        mock_save.assert_called_once_with(force=True)

    assert not coordinator.data
    assert not coordinator._persisted_etags
    assert not coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_git_diff_empty_data(hass, mock_coordinator):
    """Test that git diff methods handle empty self.data correctly."""
    coordinator = mock_coordinator
    coordinator.data = {}

    assert await coordinator.async_get_git_diff("any_path") is None
    assert coordinator.get_cached_git_diff("any_path", "lh", "rh") is None

    coordinator.set_cached_git_diff("any_path", "lh", "rh", "diff")
    assert "any_path" not in coordinator.data


def test_update_error_state_with_missing_path(mock_coordinator) -> None:
    """_update_error_state should be a no-op when the path is not present in data."""
    mock_coordinator.data = {}

    mock_coordinator._update_error_state("nonexistent/path", "test_error", "detail")
    assert not mock_coordinator.data


def test_update_error_state_with_existing_path(mock_coordinator) -> None:
    """_update_error_state should update/reset error state when the path exists."""
    path = "existing/path"
    mock_coordinator.data = {
        path: {
            "name": "Test",
            "remote_hash": "old_hash",
            "updatable": True,
            "last_error": None,
        }
    }

    mock_coordinator._update_error_state(path, "fetch_error", "detail", clear_etag=True)

    info = mock_coordinator.data[path]
    assert info["remote_hash"] is None
    assert info["updatable"] is False
    assert isinstance(info["last_error"], str)
    assert "fetch_error|detail" in info["last_error"]
    assert info["etag"] is None
