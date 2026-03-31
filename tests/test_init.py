import hashlib
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.__init__ import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.blueprints_updater.const import DOMAIN


async def test_setup_entry(hass: HomeAssistant):
    """Test setting up the entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {"old_config": "value"}

    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    with patch(
        "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        assert await async_setup_entry(hass, entry) is True

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]
    assert hass.config_entries.async_update_entry.called
    assert hass.config_entries.async_forward_entry_setups.called


async def test_service_registration(hass: HomeAssistant):
    """Test that services are registered."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = MappingProxyType(
        {
            "max_backups": 3,
        }
    )
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
    ):
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        calls = [
            call.args[2] if len(call.args) > 2 else call.kwargs.get("service")
            for call in mock_register.call_args_list
        ]
        assert "reload" in calls
        assert "restore_blueprint" in calls
        assert "update_all" in calls

        restore_call = next(
            call
            for call in mock_register.call_args_list
            if (len(call.args) > 2 and call.args[2] == "restore_blueprint")
            or call.kwargs.get("service") == "restore_blueprint"
        )
        schema = (
            restore_call.args[4]
            if len(restore_call.args) > 4
            else restore_call.kwargs.get("schema")
        )
        assert schema is not None
        schema({"entity_id": "update.test", "version": 1})
        schema({"entity_id": "update.test", "version": 4})


async def test_service_handlers(hass: HomeAssistant):
    """Test service handlers' logic."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_request_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
    ):
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        reload_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "reload")
                or call.kwargs.get("service") == "reload"
            ),
            None,
        )
        assert reload_handler is not None
        await reload_handler(ServiceCall(hass, DOMAIN, "reload", {}))
        assert coordinator_mock.async_request_refresh.called

        update_all_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "update_all")
                or call.kwargs.get("service") == "update_all"
            ),
            None,
        )
        assert update_all_handler is not None

        coordinator_mock.data = {
            "path1": {"updatable": True, "remote_content": "...", "last_error": None}
        }
        coordinator_mock.async_install_blueprint = AsyncMock()
        coordinator_mock.async_reload_services = AsyncMock()

        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {"backup": True}))
        assert coordinator_mock.async_install_blueprint.called


async def test_restore_blueprint_handler(hass: HomeAssistant):
    """Test restore_blueprint handler specifically."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
    ):
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        restore_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "restore_blueprint")
                or call.kwargs.get("service") == "restore_blueprint"
            ),
            None,
        )

        assert restore_handler is not None

        with pytest.raises(ServiceValidationError) as exc:
            await restore_handler(ServiceCall(hass, DOMAIN, "restore_blueprint", {}))
        assert exc.value.translation_key == "missing_entity_id"
        with patch("homeassistant.helpers.entity_registry.async_get") as mock_er:
            mock_er.return_value.async_get.return_value = None
            with pytest.raises(ServiceValidationError) as exc:
                await restore_handler(
                    ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.none"})
                )
            assert exc.value.translation_key == "invalid_entity"

            bad_entity = MagicMock()
            bad_entity.domain = "switch"
            mock_er.return_value.async_get.return_value = bad_entity
            with pytest.raises(ServiceValidationError) as exc:
                await restore_handler(
                    ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "switch.test"})
                )
            assert exc.value.translation_key == "invalid_entity"

            good_entity = MagicMock()
            good_entity.domain = "update"
            good_entity.config_entry_id = entry.entry_id
            good_entity.unique_id = "other_id"
            mock_er.return_value.async_get.return_value = good_entity
            coordinator_mock.data = {}
            with pytest.raises(ServiceValidationError) as exc:
                await restore_handler(
                    ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.test"})
                )
            assert exc.value.translation_key == "not_found"

            coordinator_mock.data = {"test.yaml": {"rel_path": "test.yaml", "updatable": True}}
            good_entity.unique_id = f"blueprint_{hashlib.sha256(b'test.yaml').hexdigest()}"
            with pytest.raises(ServiceValidationError) as exc:
                await restore_handler(
                    ServiceCall(
                        hass,
                        DOMAIN,
                        "restore_blueprint",
                        {"entity_id": "update.test", "version": 0},
                    )
                )
            assert exc.value.translation_key == "invalid_version"

            with pytest.raises(ServiceValidationError) as exc:
                await restore_handler(
                    ServiceCall(
                        hass,
                        DOMAIN,
                        "restore_blueprint",
                        {"entity_id": "update.test", "version": 5},
                    )
                )
            assert exc.value.translation_key == "invalid_version"


async def test_unload_entry(hass: HomeAssistant):
    """Test unloading the entry and service cleanup."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    coordinator_mock = MagicMock()
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_shutdown = AsyncMock()

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch.object(hass.services, "async_remove") as mock_remove,
    ):
        await async_setup_entry(hass, entry)
        await async_unload_entry(hass, entry)

        assert entry.entry_id not in hass.data[DOMAIN]
        assert mock_remove.called
