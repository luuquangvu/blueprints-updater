"""Tests for Blueprints Updater coordinator core functionality."""

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)

from .protocols import (
    BlueprintCoordinatorInternal,
    BlueprintCoordinatorProtocol,
    BlueprintCoordinatorPublic,
)


def test_coordinator_protocol_conformance(coordinator):
    """Verify that BlueprintUpdateCoordinator conforms to BlueprintCoordinatorProtocol.

    This test ensures that the coordinator implementation adheres to the defined
    protocols for public, internal, and combined interfaces using runtime protocol
    checks.
    """
    assert isinstance(coordinator, BlueprintCoordinatorPublic)
    assert isinstance(coordinator, BlueprintCoordinatorInternal)
    assert isinstance(coordinator, BlueprintCoordinatorProtocol)


def test_coordinator_data_initialized_to_empty_dict(hass):
    """Confirm BlueprintUpdateCoordinator sets self.data to {} after initialization."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}

    def mock_init(self, hass, logger, **kwargs):
        self.hass = hass
        self.data = None

    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        side_effect=mock_init,
        autospec=True,
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        assert coord.data == {}


@pytest.mark.asyncio
async def test_async_setup_sanitization(coordinator):
    """Test that async_setup sanitizes corrupted or invalid storage data."""
    mock_store = MagicMock()
    coordinator._store = mock_store

    mock_store.async_load = AsyncMock(
        return_value={
            "etags": "not_a_dict",
            "remote_hashes": ["not", "a", "dict"],
        }
    )

    with patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn:
        await coordinator.async_setup()
        assert coordinator._persisted_etags == {}
        assert coordinator._persisted_hashes == {}
        assert coordinator.setup_complete
        assert mock_warn.call_count == 2
        warn_msgs = [call.args[0] for call in mock_warn.call_args_list]
        assert any("Ignoring invalid persisted etags" in msg for msg in warn_msgs)
        assert any("Ignoring invalid persisted remote_hashes" in msg for msg in warn_msgs)

    mock_store.async_load = AsyncMock(
        return_value={
            "etags": {
                "valid_key": "valid_value",
                "invalid_key": 123,
                456: "invalid_val",
            },
            "remote_hashes": {
                "valid_hash_key": "hash_val",
                "broken": None,
            },
        }
    )

    coordinator.setup_complete = False
    with patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn:
        await coordinator.async_setup()
        assert coordinator._persisted_etags == {"valid_key": "valid_value"}
        assert coordinator._persisted_hashes == {"valid_hash_key": "hash_val"}
        assert coordinator.setup_complete
        assert mock_warn.call_count == 2

        mock_warn.assert_any_call(
            "Dropped %d invalid ETag entries from storage (non-string keys or values)", 2
        )
        mock_warn.assert_any_call("Dropped %d invalid remote hash entries from storage", 1)
