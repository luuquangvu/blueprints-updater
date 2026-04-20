"""Tests for increasing coverage of Config Flow."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.config_flow import (
    BlueprintsUpdaterConfigFlow,
    BlueprintsUpdaterOptionsFlowHandler,
)


@pytest.mark.asyncio
async def test_config_flow_single_instance_abort(hass):
    """Test that config flow aborts if an instance already exists."""
    flow = BlueprintsUpdaterConfigFlow()
    flow.hass = hass

    entry = MagicMock()
    cast(Any, flow)._async_current_entries = MagicMock(return_value=[entry])

    result = await flow.async_step_user()

    assert result.get("type") == "abort"
    assert result.get("reason") == "single_instance_allowed"


@pytest.mark.asyncio
async def test_options_flow_lifecycle(hass):
    """Test the full lifecycle of the options flow."""
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.data = {}
    config_entry.options = {}

    handler = BlueprintsUpdaterOptionsFlowHandler()
    handler.hass = hass
    cast(Any, handler).handler = config_entry.entry_id

    hass.config_entries = MagicMock()
    hass.config_entries.async_get_known_entry = MagicMock(return_value=config_entry)

    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await handler.async_step_init()
        assert result.get("type") == "form"
        assert result.get("step_id") == "init"
        result = await handler.async_step_init(user_input={"auto_update": True})
        assert result.get("type") == "create_entry"
        assert result.get("data") == {"auto_update": True}
