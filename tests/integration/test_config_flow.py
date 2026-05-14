"""Test the config flow for Blueprints Updater."""

from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant

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
                "use_cdn": True,
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
