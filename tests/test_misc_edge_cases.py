"""Tests for edge cases and miscellaneous utilities across the integration."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater import async_unload_entry
from custom_components.blueprints_updater.config_flow import _async_get_blueprint_options
from custom_components.blueprints_updater.const import (
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.utils import (
    get_config_int,
    get_max_backups,
    get_update_interval,
    retry_async,
)


@pytest.fixture
def coordinator(hass: HomeAssistant):
    """Mock coordinator."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__", return_value=None
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord.config_entry = entry
        coord.setup_complete = True
        return coord


@pytest.mark.asyncio
async def test_init_unload_path(hass: HomeAssistant):
    """Test unloading entry in __init__.py."""
    entry = MagicMock()
    entry.entry_id = "test_entry"

    mock_coord = AsyncMock()
    hass.data[DOMAIN] = {"coordinators": {"test_entry": mock_coord}}

    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True
    assert "test_entry" not in hass.data[DOMAIN]["coordinators"]


@pytest.mark.asyncio
async def test_coordinator_misc(coordinator: BlueprintUpdateCoordinator):
    """Test misc coordinator paths."""
    coordinator._refresh_lock = MagicMock()
    coordinator._refresh_lock.locked.return_value = True
    await coordinator._async_background_refresh({})

    mock_task = MagicMock()
    mock_task.done.return_value = False
    coordinator._background_task = mock_task
    coordinator._async_cancel_background_task()
    mock_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_scanning(hass: HomeAssistant):
    """Test config flow scanning."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator.scan_blueprints"
        ) as mock_scan,
        patch.object(hass.config, "path", return_value="blueprints"),
    ):
        mock_scan.return_value = {"blueprints/automation/test.yaml": {"name": "Test BP"}}
        options = await _async_get_blueprint_options(hass)
        assert len(options) == 1


@pytest.mark.asyncio
async def test_utils_behavior():
    """Test utils behavior."""
    assert get_config_int("NOT_A_DICT_OR_OBJ", "key", 10) == 10
    assert get_update_interval(None) == 24
    assert get_update_interval({CONF_UPDATE_INTERVAL: 12}) == 12

    entry = MagicMock()
    entry.options = {CONF_UPDATE_INTERVAL: 15}
    entry.data = {}
    assert get_update_interval(entry) == 15

    assert get_max_backups(None) == 3
    assert get_max_backups({CONF_MAX_BACKUPS: 5}) == 5

    mock_calls = 0

    async def mock_func(*args, **kwargs):
        nonlocal mock_calls
        mock_calls += 1
        raise RuntimeError("Fail")

    with pytest.raises(RuntimeError, match="Fail"):
        await retry_async(max_retries=2, exceptions=(RuntimeError,))(mock_func)()
    assert mock_calls == 3
