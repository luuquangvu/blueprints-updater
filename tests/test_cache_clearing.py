"""Tests for translation cache clearing."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event

from custom_components.blueprints_updater import DOMAIN, async_setup


@pytest.mark.asyncio
async def test_clear_cache_invalidates_coordinator_translations(hass):
    """Test that _clear_cache invalidates coordinator translations."""
    with patch("custom_components.blueprints_updater._async_register_services"):
        await async_setup(hass, {})

    assert hass.bus.async_listen.called
    args, _ = hass.bus.async_listen.call_args
    assert args[0] == EVENT_CORE_CONFIG_UPDATE
    clear_cache_func = args[1]

    mock_coordinator = MagicMock()

    hass.data[DOMAIN] = {
        "translation_cache": {("en", "common"): {"key": "value"}},
        "coordinators": {"test_entry": mock_coordinator},
    }

    clear_cache_func(Event(EVENT_CORE_CONFIG_UPDATE))

    assert not hass.data[DOMAIN]["translation_cache"]
    assert mock_coordinator.clear_translations.called
