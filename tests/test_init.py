from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import ServiceCall

from custom_components.blueprints_updater.__init__ import async_setup_entry
from custom_components.blueprints_updater.const import DOMAIN


@pytest.mark.asyncio
async def test_update_all_service(hass):
    """Test the update_all service logic."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.options = {}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()

    coordinator_mock = MagicMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_install_blueprint = AsyncMock()
    coordinator_mock.async_reload_services = AsyncMock()
    coordinator_mock.async_request_refresh = AsyncMock()

    coordinator_mock.data = {
        "file1.yaml": {"updatable": True, "remote_content": "content1", "last_error": None},
        "file2.yaml": {"updatable": True, "remote_content": "content2", "last_error": None},
        "file3.yaml": {
            "updatable": True,
            "remote_content": None,
            "last_error": None,
        },
        "file4.yaml": {
            "updatable": True,
            "remote_content": "content4",
            "last_error": "Syntax Error",
        },
        "file5.yaml": {
            "updatable": False,
            "remote_content": "content5",
            "last_error": None,
        },
    }

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.translation.async_get_translations",
            return_value={},
        ),
    ):
        await async_setup_entry(hass, entry)

    assert hass.services.async_register.call_count >= 3

    update_all_handler = None
    for call in hass.services.async_register.call_args_list:
        if call.args[1] == "update_all":
            update_all_handler = call.args[2]
            break

    assert update_all_handler is not None

    service_call = ServiceCall(hass, DOMAIN, "update_all", {"backup": True})
    await update_all_handler(service_call)

    assert coordinator_mock.async_install_blueprint.call_count == 2
    coordinator_mock.async_install_blueprint.assert_any_call(
        "file1.yaml", "content1", reload_services=False, backup=True
    )
    coordinator_mock.async_install_blueprint.assert_any_call(
        "file2.yaml", "content2", reload_services=False, backup=True
    )

    coordinator_mock.async_reload_services.assert_called_once()
    coordinator_mock.async_request_refresh.assert_called_once()

    coordinator_mock.async_install_blueprint.reset_mock()
    coordinator_mock.async_reload_services.reset_mock()
    coordinator_mock.data = {"file1.yaml": {"updatable": False}}

    await update_all_handler(service_call)
    coordinator_mock.async_install_blueprint.assert_not_called()
    coordinator_mock.async_reload_services.assert_not_called()
