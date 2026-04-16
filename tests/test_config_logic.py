"""Tests for BlueprintUpdateCoordinator configuration helpers."""

from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.blueprints_updater.config_flow import _get_config_schema
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

    coordinator = create_mock_coordinator(hass, entry)
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

    coordinator = create_mock_coordinator(hass, entry)
    assert coordinator.is_cdn_enabled() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({CONF_USE_CDN: False}, False),
        ({CONF_USE_CDN: True}, True),
    ],
)
async def test_is_cdn_enabled_fallback_logic(hass, data, expected):
    """Test is_cdn_enabled falls back to data when options are missing it."""
    entry = MagicMock()
    entry.data = data
    entry.options = {}

    coordinator = create_mock_coordinator(hass, entry)
    assert coordinator.is_cdn_enabled() is expected


@pytest.mark.asyncio
async def test_config_helpers_no_entry(hass):
    """Test config helpers handle missing config_entry."""
    coordinator = create_mock_coordinator(hass, None)
    assert coordinator.is_auto_update_enabled() is DEFAULT_AUTO_UPDATE
    assert coordinator.is_cdn_enabled() is DEFAULT_USE_CDN


def create_mock_coordinator(
    hass, entry: Any | None, interval: timedelta = timedelta(hours=24)
) -> BlueprintUpdateCoordinator:
    """Helper to create a BlueprintUpdateCoordinator under patch."""
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        return BlueprintUpdateCoordinator(hass, cast(Any, entry), interval)


def get_schema_default(schema: vol.Schema, key_name: str) -> Any:
    """Safely extract default value from a Voluptuous schema for a given key string."""
    for key, _ in schema.schema.items():
        k_any: Any = key
        name = k_any if isinstance(k_any, str) else str(getattr(k_any, "schema", ""))
        if name == key_name:
            if not hasattr(k_any, "default"):
                return None
            attr_name = "default"
            default = getattr(k_any, attr_name)
            return default() if callable(default) else default
    raise KeyError(f"Key {key_name} not found in schema")


@pytest.mark.asyncio
async def test_get_config_schema_entry_precedence(hass):
    """Test that _get_config_schema uses options over data for defaults."""
    entry = MagicMock()
    entry.data = {
        CONF_AUTO_UPDATE: False,
        CONF_USE_CDN: False,
    }
    entry.options = {
        CONF_AUTO_UPDATE: True,
        CONF_USE_CDN: True,
    }

    schema = _get_config_schema(entry, [])

    assert get_schema_default(schema, CONF_AUTO_UPDATE) is True
    assert get_schema_default(schema, CONF_USE_CDN) is True


@pytest.mark.asyncio
async def test_get_config_schema_fallback_to_data(hass):
    """Test that _get_config_schema falls back to data when options are missing."""
    entry = MagicMock()
    entry.data = {
        CONF_AUTO_UPDATE: False,
        CONF_USE_CDN: False,
    }
    entry.options = {}

    schema = _get_config_schema(entry, [])

    assert get_schema_default(schema, CONF_AUTO_UPDATE) is False
    assert get_schema_default(schema, CONF_USE_CDN) is False


@pytest.mark.asyncio
async def test_get_config_schema_initial_defaults(hass):
    """Test that _get_config_schema falls back to system defaults for initial config."""
    schema = _get_config_schema({}, [])

    assert get_schema_default(schema, CONF_AUTO_UPDATE) is DEFAULT_AUTO_UPDATE
    assert get_schema_default(schema, CONF_USE_CDN) is DEFAULT_USE_CDN
