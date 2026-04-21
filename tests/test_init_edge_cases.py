"""Tests targeting edge cases and setup failures in the integration initialization."""

from datetime import timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater import async_setup, async_setup_entry, async_unload_entry
from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


async def async_raise_gen_err(*args, **kwargs) -> None:
    """Helper to raise RuntimeError in an async context."""
    raise RuntimeError("e")


async def _async_none(*args, **kwargs) -> None:
    """Mock async function returning None."""
    pass


async def _async_true(*args, **kwargs) -> bool:
    """Mock async function returning True."""
    return True


@pytest.mark.asyncio
async def test_initialization_lifecycle_handling(hass: HomeAssistant) -> None:
    """Test the initialization lifecycle.

    Includes service registration and core configuration event handling.
    The service registration check handles variants where service name and
    handler could be positional arguments or keyword arguments.
    """
    hass.data.clear()

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    entry.domain = DOMAIN
    entry.options = {}
    entry.data = {}

    coordinator = MagicMock()
    coordinator.config_entry = entry
    coordinator.async_setup = AsyncMock(side_effect=_async_none)
    coordinator.async_config_entry_first_refresh = AsyncMock(side_effect=_async_none)
    coordinator.async_fetch_blueprint = AsyncMock(side_effect=_async_none)
    coordinator.async_install_blueprint = AsyncMock(side_effect=_async_none)
    coordinator.async_shutdown = AsyncMock(side_effect=_async_none)
    coordinator.data = {"test.yaml": {"rel_path": "test.yaml", "updatable": True}}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=_async_none)
    hass.config_entries.async_unload_platforms = AsyncMock(side_effect=_async_true)

    import custom_components.blueprints_updater as bp_updater

    init_path = "custom_components.blueprints_updater.__init__"
    with (
        patch(f"{init_path}.BlueprintUpdateCoordinator", return_value=coordinator),
        patch.object(bp_updater, "async_register_admin_service") as mock_register,
    ):
        await async_setup(hass, {})

        mock_listen = cast(MagicMock, hass.bus.async_listen)
        callback = next(
            call.args[1]
            for call in mock_listen.call_args_list
            if call.args[0] == EVENT_CORE_CONFIG_UPDATE
        )
        callback(MagicMock())

        hass.data.setdefault(DOMAIN, {})
        callback(MagicMock())

        await async_setup_entry(hass, entry)
        await async_unload_entry(hass, entry)

        hass.config_entries.async_forward_entry_setups.side_effect = Exception("Setup fail")
        with pytest.raises(Exception, match="Setup fail"):
            await async_setup_entry(hass, entry)

        update_all_handler = None
        for call in mock_register.call_args_list:
            service_name = call.args[2] if len(call.args) > 2 else call.kwargs.get("service")
            if service_name == "update_all":
                update_all_handler = (
                    call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                )
                break
        assert update_all_handler is not None

        hass.data[DOMAIN]["coordinators"] = {}
        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))

        coordinator.data = {}
        hass.data[DOMAIN]["coordinators"] = {"entry": coordinator}
        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))

        coordinator.data = {"test.yaml": {"rel_path": "test.yaml", "updatable": True}}
        coordinator.config_entry = None
        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))
        coordinator.data = {
            "test.yaml": {"rel_path": "test.yaml", "updatable": True, "remote_content": "new_bp:"}
        }
        coordinator.config_entry = entry
        with patch.object(
            coordinator, "async_reload_services", side_effect=Exception("Update fail")
        ) as mock_reload:
            await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))
            mock_reload.assert_called_once()

            with (
                patch.object(
                    bp_updater, "async_register_admin_service", side_effect=Exception("Reg fail")
                ),
                pytest.raises(Exception, match="Reg fail"),
            ):
                await async_setup(hass, {})


@pytest.mark.asyncio
async def test_coordinator_error_paths_fetch_refresh_and_configs(hass: HomeAssistant) -> None:
    """Target specific uncovered lines in coordinator.py."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    with patch("homeassistant.helpers.frame.report_usage"):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=1))

    coordinator.data = {"p": {"local_hash": "abc", "rel_path": "path"}}

    res: list[str] = []
    dom: set[str] = set()
    with patch.object(coordinator, "_detect_risks_for_update", return_value=[]):
        await coordinator._process_blueprint_content(
            "p", coordinator.data["p"], "invalid: yaml: :", "e", "u", res, dom
        )

    mock_resp = MagicMock()
    mock_resp.is_redirect = False
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json = MagicMock(side_effect=ValueError("JSON fail"))

    coord_path = "custom_components.blueprints_updater.coordinator"
    prov_path = "custom_components.blueprints_updater.providers"

    async def _async_get(*args, **kwargs):
        return mock_resp

    with patch(f"{coord_path}.get_async_client") as mock_client:
        mock_client.return_value.get = AsyncMock(side_effect=_async_get)
        with patch(f"{prov_path}.ProviderRegistry.get_provider") as mock_get:
            mock_get.return_value = MagicMock()
            with pytest.raises(HomeAssistantError, match="Invalid JSON response"):
                await coordinator._async_fetch_content(mock_client.return_value, "mock_url")

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator.async_config_entry_first_refresh",
        new_callable=AsyncMock,
    ) as mock_refresh:
        mock_refresh.side_effect = Exception("Refresh fail")
        entry = MagicMock()
        entry.data = {}
        entry.options = {"update_interval": 24}
        entry.entry_id = "test_entry"

        with (
            patch(
                "custom_components.blueprints_updater.BlueprintUpdateCoordinator",
                return_value=coordinator,
            ),
            pytest.raises(Exception, match="Refresh fail"),
        ):
            await async_setup_entry(hass, entry)

    mock_comp = MagicMock()
    mock_comp.get_entity.return_value = None
    hass.data["automation"] = mock_comp
    assert coordinator._get_entities_configs(["automation.missing"]) == {}

    await coordinator.async_reload_services(None)
