"""Tests for Blueprints Updater initialization."""

from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.__init__ import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
    async_update_options,
)
from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


def _setup_test_coordinator(hass: HomeAssistant, entry_id: str, coordinator: Any) -> None:
    """Register a coordinator in hass.data for testing."""
    hass.data.setdefault(DOMAIN, {}).setdefault("coordinators", {})[entry_id] = coordinator


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
    assert entry.entry_id in hass.data[DOMAIN]["coordinators"]
    assert hass.config_entries.async_update_entry.called
    assert hass.config_entries.async_forward_entry_setups.called


async def test_service_registration(hass: HomeAssistant):
    """Test that services are registered."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = MappingProxyType(
        {
            "max_backups": 3,
        }
    )

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    _setup_test_coordinator(hass, entry.entry_id, coordinator_mock)

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        calls = [
            call.args[2] if len(call.args) > 2 else call.kwargs.get("service")
            for call in mock_register.call_args_list
        ]
        assert "reload" in calls
        assert "restore_blueprint" in calls
        assert "update_all" in calls
        assert hass.data[DOMAIN].get("services_registered") is True

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

    _setup_test_coordinator(hass, entry.entry_id, coordinator_mock)

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
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

    _setup_test_coordinator(hass, entry.entry_id, coordinator_mock)

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ) as mock_coordinator_class,
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        mock_coordinator_class.generate_unique_id = BlueprintUpdateCoordinator.generate_unique_id
        mock_coordinator_class.generate_legacy_unique_id = (
            BlueprintUpdateCoordinator.generate_legacy_unique_id
        )
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
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
            good_entity.unique_id = BlueprintUpdateCoordinator.generate_unique_id(
                "test_entry", "test.yaml"
            )
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

            legacy_id = BlueprintUpdateCoordinator.generate_legacy_unique_id("test.yaml")
            good_entity.unique_id = legacy_id
            coordinator_mock.async_restore_blueprint = AsyncMock(
                return_value={"success": True, "translation_key": "success"}
            )
            coordinator_mock.async_request_refresh = AsyncMock()
            coordinator_mock.async_translate = AsyncMock(return_value="Success")

            result = await restore_handler(
                ServiceCall(
                    hass,
                    DOMAIN,
                    "restore_blueprint",
                    {"entity_id": "update.test", "version": 1},
                )
            )
            assert result["success"] is True
            assert coordinator_mock.async_restore_blueprint.called


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
        patch.object(hass.services, "has_service", return_value=False),
        patch.object(hass.services, "async_remove") as mock_remove,
    ):
        await async_setup_entry(hass, entry)

        cast(MagicMock, hass.services.has_service).return_value = True
        await async_unload_entry(hass, entry)

        assert entry.entry_id not in hass.data[DOMAIN].get("coordinators", {})
        assert mock_remove.called
        assert hass.data[DOMAIN].get("services_registered") is False
        coordinator_mock.async_shutdown.assert_awaited_once()

    hass.data[DOMAIN]["services_registered"] = True
    hass.data[DOMAIN].setdefault("coordinators", {})[entry.entry_id] = coordinator_mock
    with (
        patch.object(hass.services, "has_service", return_value=False),
        patch("custom_components.blueprints_updater.__init__._LOGGER") as mock_logger,
    ):
        await async_unload_entry(hass, entry)
        assert mock_logger.debug.called
        assert hass.data[DOMAIN].get("services_registered") is False


@pytest.mark.asyncio
async def test_restore_handler_multi_coordinator_selection(hass: HomeAssistant):
    """Ensure restore handler selects the correct coordinator based on entity's config_entry_id."""
    entry_one = MagicMock()
    entry_one.entry_id = "entry_one"
    entry_one.options = {}
    entry_one.data = {}
    entry_two = MagicMock()
    entry_two.entry_id = "entry_two"
    entry_two.options = {}
    entry_two.data = {}

    coordinator_one = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_one.config_entry = entry_one
    coordinator_one.data = {}

    coordinator_two = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_two.config_entry = entry_two
    coordinator_two.data = {}

    _setup_test_coordinator(hass, entry_one.entry_id, coordinator_one)
    _setup_test_coordinator(hass, entry_two.entry_id, coordinator_two)

    mock_entity_registry = MagicMock()

    with (
        patch("homeassistant.core.async_get_hass_or_none", return_value=hass),
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator"
        ) as mock_coord_class,
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
        patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_entity_registry),
    ):
        mock_coord_class.return_value = coordinator_one
        mock_coord_class.generate_unique_id = BlueprintUpdateCoordinator.generate_unique_id
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        await async_setup(hass, {})
        await async_setup_entry(hass, entry_one)

        mock_coord_class.return_value = coordinator_two
        await async_setup_entry(hass, entry_two)

        restore_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "restore_blueprint")
                or call.kwargs.get("service") == "restore_blueprint"
            ),
            None,
        )

        entity_entry = MagicMock()
        entity_entry.domain = "update"
        entity_entry.config_entry_id = "entry_two"
        entity_entry.unique_id = BlueprintUpdateCoordinator.generate_unique_id(
            "entry_two", "two.yaml"
        )
        mock_entity_registry.async_get.return_value = entity_entry

        coordinator_two.data = {"two.yaml": {"rel_path": "two.yaml"}}
        coordinator_two.async_restore_blueprint = AsyncMock(
            return_value={"success": True, "message": "Success"}
        )

        assert restore_handler is not None
        await restore_handler(
            ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.two"})
        )
        coordinator_two.async_restore_blueprint.assert_called_once()
        coordinator_one.async_restore_blueprint.assert_not_called()

        hass.data[DOMAIN]["coordinators"].pop("entry_two")
        assert restore_handler is not None
        with pytest.raises(ServiceValidationError) as exc:
            await restore_handler(
                ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.two"})
            )
        assert exc.value.translation_key == "not_found"


@pytest.mark.asyncio
async def test_async_update_all_handler_fetches_remote_content(hass: HomeAssistant):
    """Ensure update_all fetches missing remote content if updatable is True."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}

    coordinator = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator.config_entry = entry
    coordinator.data = {
        "test.yaml": {
            "rel_path": "test.yaml",
            "updatable": True,
            "remote_content": None,
        }
    }

    _setup_test_coordinator(hass, entry.entry_id, coordinator)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {
        "test.yaml": {
            "rel_path": "test.yaml",
            "updatable": True,
            "remote_content": None,
        }
    }
    mock_coordinator.async_setup = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=mock_coordinator,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        update_all_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "update_all")
                or call.kwargs.get("service") == "update_all"
            ),
            None,
        )

        mock_coordinator.async_fetch_blueprint = AsyncMock()
        mock_coordinator.async_install_blueprint = AsyncMock()
        mock_coordinator.async_reload_services = AsyncMock()
        mock_coordinator.async_request_refresh = AsyncMock()

        assert update_all_handler is not None
        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))

        mock_coordinator.async_fetch_blueprint.assert_called_once_with("test.yaml", force=True)


@pytest.mark.asyncio
async def test_async_update_all_handler_continues_on_failure(hass: HomeAssistant):
    """Ensure update_all continues to next blueprint if one fails."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}

    mock_coordinator = MagicMock()
    mock_coordinator.config_entry = entry
    mock_coordinator.data = {
        "fail.yaml": {
            "rel_path": "fail.yaml",
            "updatable": True,
            "remote_content": "...",
            "last_error": None,
        },
        "success.yaml": {
            "rel_path": "success.yaml",
            "updatable": True,
            "remote_content": "...",
            "last_error": None,
        },
    }

    _setup_test_coordinator(hass, entry.entry_id, mock_coordinator)

    mock_coordinator.async_setup = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=mock_coordinator,
        ),
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        update_all_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "update_all")
                or call.kwargs.get("service") == "update_all"
            ),
            None,
        )

        async def mock_install(path: str, *args: Any, **kwargs: Any):
            """Mock install blueprint."""
            if path == "fail.yaml":
                raise ValueError("Update failed")
            return

        mock_coordinator.async_install_blueprint = AsyncMock(side_effect=mock_install)
        mock_coordinator.async_reload_services = AsyncMock()
        mock_coordinator.async_request_refresh = AsyncMock()

        assert update_all_handler is not None
        await update_all_handler(ServiceCall(hass, DOMAIN, "update_all", {}))

        assert mock_coordinator.async_install_blueprint.call_count == 2

        mock_coordinator.async_reload_services.assert_called_once()
        mock_coordinator.async_request_refresh.assert_called_once()


async def test_async_update_options_refreshes_coordinator(hass: HomeAssistant):
    """Test that async_update_options refreshes the coordinator's config entry and interval."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {"update_interval": 12}

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.config_entry = MagicMock()
    coordinator_mock.async_request_refresh = AsyncMock()

    _setup_test_coordinator(hass, entry.entry_id, coordinator_mock)

    await async_update_options(hass, entry)

    assert coordinator_mock.config_entry == entry
    assert coordinator_mock.update_interval == timedelta(hours=12)
    assert coordinator_mock.async_request_refresh.called


@pytest.mark.asyncio
async def test_init_unload_path(hass: HomeAssistant):
    """Test unloading entry in __init__.py."""
    entry = MagicMock()
    entry.entry_id = "test_entry"

    mock_coord = MagicMock()
    mock_coord.async_shutdown = AsyncMock()
    hass.data[DOMAIN] = {"coordinators": {"test_entry": mock_coord}}

    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True
    assert "test_entry" not in hass.data[DOMAIN]["coordinators"]
    mock_coord.async_shutdown.assert_awaited_once()
