"""Tests for Blueprints Updater config flow."""

import os
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.config_flow import (
    BlueprintsUpdaterConfigFlow,
    BlueprintsUpdaterOptionsFlowHandler,
    _async_get_blueprint_options,
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
            if key.default is vol.UNDEFINED:
                continue
            key_str = str(key)
            defaults[key_str] = key.default() if callable(key.default) else key.default
    return defaults


def get_data_schema(result: ConfigFlowResult) -> vol.Schema:
    """Extract and validate data_schema from a ConfigFlowResult.

    Args:
        result: The result to extract from.

    Returns:
        The validated voluptuous Schema.

    """
    data_schema = result.get("data_schema")
    assert isinstance(data_schema, vol.Schema)
    return data_schema


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
        result: ConfigFlowResult = await flow.async_step_user()
        assert result.get("type") == "form"
        assert result.get("step_id") == "user"

        data_schema = get_data_schema(result)
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
        result: ConfigFlowResult = await handler.async_step_init()
        assert result.get("type") == "form"

        data_schema = get_data_schema(result)
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
        result: ConfigFlowResult = await handler.async_step_init()
        data_schema = get_data_schema(result)
        defaults = get_schema_defaults(data_schema)
        assert defaults.get(CONF_MAX_BACKUPS) == 1
        assert defaults.get(CONF_UPDATE_INTERVAL) == 24


@pytest.mark.asyncio
async def test_options_flow_safe_coercion(hass: HomeAssistant):
    """Test that the options flow safely coerces None or string values."""
    config_entry = MagicMock()
    config_entry.options = {
        CONF_MAX_BACKUPS: "8",
        CONF_UPDATE_INTERVAL: None,
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
        result: ConfigFlowResult = await handler.async_step_init()
        assert result.get("type") == "form"

        data_schema = get_data_schema(result)
        defaults = get_schema_defaults(data_schema)

        assert defaults.get(CONF_MAX_BACKUPS) == 8
        assert defaults.get(CONF_UPDATE_INTERVAL) == 24


@pytest.mark.asyncio
async def test_options_flow_enhanced_coercion(hass: HomeAssistant):
    """Test that the options flow handles whitespace and negative string values."""
    config_entry = MagicMock()
    config_entry.options = {
        CONF_MAX_BACKUPS: " -5 ",
        CONF_UPDATE_INTERVAL: "  10  ",
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
        result: ConfigFlowResult = await handler.async_step_init()
        data_schema = get_data_schema(result)
        defaults = get_schema_defaults(data_schema)

        assert defaults.get(CONF_MAX_BACKUPS) == 1
        assert defaults.get(CONF_UPDATE_INTERVAL) == 10


@pytest.mark.asyncio
async def test_config_flow_scanning(hass: HomeAssistant):
    """Test config flow scanning."""
    base_path = os.path.abspath("blueprints")
    full_path = os.path.join(base_path, "automation/test.yaml")

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator.scan_blueprints"
        ) as mock_scan,
        patch.object(hass.config, "path", return_value=base_path),
    ):
        mock_scan.return_value = {
            full_path: {
                "name": "Test BP",
                "domain": "automation",
                "source_url": "https://example.com/test.yaml",
                "local_hash": "hash123",
                "rel_path": "automation/test.yaml",
            }
        }
        options = await _async_get_blueprint_options(hass)
        assert len(options) == 1
        assert options[0]["value"] == "automation/test.yaml"
        assert options[0]["label"] == "Test BP [automation/test.yaml]"
