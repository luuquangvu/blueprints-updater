"""Tests for Blueprints Updater coordinator core functionality."""

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)


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
async def test_coordinator_translation_format_error(coordinator):
    """Test translation formatting error handling."""
    coordinator.hass.config.language = "en"

    coordinator._translations = {
        ("en", "common"): {"component.blueprints_updater.common.test": "Hello {name}"}
    }
    coordinator.setup_complete = True

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        side_effect=Exception("Should not be called"),
    ) as mock_get_translations:
        result = await coordinator.async_translate("test", category="common", username="x")
        assert result == "Hello {name}"
        mock_get_translations.assert_not_called()
