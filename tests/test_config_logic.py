"""Tests for BlueprintUpdateCoordinator configuration helpers."""

from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import (
    CONF_AUTO_UPDATE,
    CONF_USE_CDN,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_USE_CDN,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "options", "expected"),
    [
        ({}, {}, DEFAULT_AUTO_UPDATE),
        ({CONF_AUTO_UPDATE: False}, {}, False),
        ({CONF_AUTO_UPDATE: True}, {}, True),
        ({CONF_AUTO_UPDATE: False}, {CONF_AUTO_UPDATE: True}, True),
        ({CONF_AUTO_UPDATE: True}, {CONF_AUTO_UPDATE: False}, False),
    ],
)
async def test_is_auto_update_enabled_config_logic(hass, data, options, expected):
    """Test is_auto_update_enabled respects default and config_entry precedence."""
    entry = MagicMock()
    entry.data = data
    entry.options = options

    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        assert coordinator.is_auto_update_enabled() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, DEFAULT_USE_CDN),
        ({CONF_USE_CDN: False}, False),
        ({CONF_USE_CDN: True}, True),
    ],
)
async def test_is_cdn_enabled_config_logic(hass, options, expected):
    """Test is_cdn_enabled respects default and options."""
    entry = MagicMock()
    entry.data = {}
    entry.options = options

    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        assert coordinator.is_cdn_enabled() is expected


@pytest.mark.asyncio
async def test_config_helpers_no_entry(hass):
    """Test config helpers handle missing config_entry."""
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coordinator = BlueprintUpdateCoordinator(hass, cast(Any, None), timedelta(hours=24))
        assert coordinator.is_auto_update_enabled() is DEFAULT_AUTO_UPDATE
        assert coordinator.is_cdn_enabled() is DEFAULT_USE_CDN
