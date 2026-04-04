"""Tests for Blueprints Updater config flow."""

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.config_flow import (
    BlueprintsUpdaterConfigFlow,
    BlueprintsUpdaterOptionsFlowHandler,
)
from custom_components.blueprints_updater.const import (
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
)


def get_schema_defaults(schema: vol.Schema) -> dict[str, Any]:
    """Extract default values from a voluptuous schema.

    Args:
        schema: The voluptuous schema to extract defaults from.

    Returns:
        A dictionary mapping schema keys to their default values.

    """
    defaults = {}
    for key in schema.schema:
        if hasattr(key, "default"):
            key_str = str(key)
            defaults[key_str] = key.default() if callable(key.default) else key.default
    return defaults


@pytest.mark.asyncio
async def test_config_flow_defaults(hass: HomeAssistant):
    """Test that the config flow shows correct defaults."""
    flow = BlueprintsUpdaterConfigFlow()
    flow.hass = hass

    cast(Any, flow)._async_current_entries = MagicMock(return_value=[])

    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        return_value=[],
    ):
        result = await flow.async_step_user()
        assert result["type"] == "form"
        assert result["step_id"] == "user"

        data_schema = result["data_schema"]
        assert isinstance(data_schema, vol.Schema)
        defaults = get_schema_defaults(data_schema)
        assert defaults.get(CONF_UPDATE_INTERVAL) == 24
        assert defaults.get(CONF_MAX_BACKUPS) == 3


@pytest.mark.asyncio
async def test_options_flow_clamping(hass: HomeAssistant):
    """Test that the options flow clamps out-of-bounds values from existing entries."""
    config_entry = MagicMock()
    config_entry.options = {
        CONF_MAX_BACKUPS: 15,
        CONF_UPDATE_INTERVAL: 0,
    }
    config_entry.entry_id = "test_entry"

    handler = BlueprintsUpdaterOptionsFlowHandler()
    handler.hass = hass

    cast(Any, handler).handler = config_entry.entry_id

    hass.config_entries = MagicMock()
    hass.config_entries.async_get_known_entry = MagicMock(return_value=config_entry)

    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        return_value=[],
    ):
        result = await handler.async_step_init()
        assert result["type"] == "form"

        data_schema = result["data_schema"]
        assert isinstance(data_schema, vol.Schema)
        defaults = get_schema_defaults(data_schema)

        assert defaults.get(CONF_UPDATE_INTERVAL) == 1
        assert defaults.get(CONF_MAX_BACKUPS) == 10

    config_entry.options = {
        CONF_MAX_BACKUPS: -5,
        CONF_UPDATE_INTERVAL: 24,
    }
    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        return_value=[],
    ):
        result = await handler.async_step_init()
        data_schema = result["data_schema"]
        assert isinstance(data_schema, vol.Schema)
        defaults = get_schema_defaults(data_schema)
        assert defaults.get(CONF_MAX_BACKUPS) == 1
        assert defaults.get(CONF_UPDATE_INTERVAL) == 24
