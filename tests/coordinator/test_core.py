"""Tests for Blueprints Updater coordinator core functionality."""

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import MagicMock, patch

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
