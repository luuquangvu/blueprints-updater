"""Tests for config flow coverage."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.config_flow import (
    BlueprintsUpdaterConfigFlow,
)


@pytest.mark.asyncio
async def test_config_flow_user_step():
    """Test the user step of the config flow."""
    flow = BlueprintsUpdaterConfigFlow()
    flow.hass = MagicMock()

    mock_options = AsyncMock(return_value=[{"value": "bp.yaml", "label": "BP"}])
    with (
        patch.object(flow, "_async_current_entries", return_value=[]),
        patch(
            "custom_components.blueprints_updater.config_flow._async_get_blueprint_options",
            mock_options,
        ),
    ):
        result = await flow.async_step_user()
        res_dict = cast(dict[str, Any], result)
        assert res_dict["type"] == "form"
        assert res_dict["step_id"] == "user"

        with patch.object(flow, "async_set_unique_id"):
            result = await flow.async_step_user(user_input={})
            res_dict = cast(dict[str, Any], result)
            assert res_dict["type"] == "create_entry"
            assert res_dict["title"] == "Blueprints Updater"
