"""Test the config flow for Blueprints Updater."""

from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
)


@pytest.mark.asyncio
async def test_config_flow_user_step(hass: HomeAssistant) -> None:
    """Test the user step of the config flow."""
    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "user"

    with patch(
        "custom_components.blueprints_updater.async_setup_entry",
        return_value=True,
    ) as mock_setup:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "auto_update": True,
                "update_interval": 24,
                "max_backups": 5,
                "filter_mode": "all",
                "selected_blueprints": [],
            },
        )
        await hass.async_block_till_done()

    assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2.get("title") == "Blueprints Updater"
    assert result2.get("options", {}).get("update_interval") == 24
    assert len(mock_setup.mock_calls) == 1


@pytest.mark.asyncio
async def test_options_flow_integration(hass: HomeAssistant) -> None:
    """Test the options flow lifecycle through Home Assistant's options manager."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "auto_update": False,
            "update_interval": 24,
            "max_backups": 5,
            "filter_mode": "all",
            "selected_blueprints": [],
        },
        entry_id="test_options_entry",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
        return_value=[],
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "auto_update": True,
            "update_interval": 12,
            "max_backups": 3,
            "filter_mode": "all",
            "selected_blueprints": [],
        },
    )
    await hass.async_block_till_done()

    assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options["update_interval"] == 12
    assert entry.options["auto_update"] is True
    assert entry.options["max_backups"] == 3
