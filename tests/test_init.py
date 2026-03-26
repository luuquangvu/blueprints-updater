import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import ServiceCall

from custom_components.blueprints_updater.__init__ import (
    async_setup_entry,
    async_unload_entry,
)
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
    coordinator_mock.config_entry = entry
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

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

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

        update_all_handler = next(
            (
                call.args[2]
                for call in hass.services.async_register.call_args_list
                if call.args[1] == "update_all"
            ),
            None,
        )

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


@pytest.mark.asyncio
async def test_restore_blueprint_service(hass):
    """Test the restore_blueprint service handler and translation."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.data = {
        "/config/blueprints/test.yaml": {
            "name": "Test",
            "rel_path": "test.yaml",
        }
    }

    coordinator_mock.async_restore_blueprint = AsyncMock(
        return_value={"success": False, "translation_key": "missing_backup"}
    )

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    entity_entry = MagicMock()
    entity_entry.domain = "update"
    entity_entry.config_entry_id = entry.entry_id
    entity_entry.unique_id = f"blueprint_{hashlib.sha256(b'test.yaml').hexdigest()}"

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch("custom_components.blueprints_updater.__init__.er.async_get") as mock_er,
        patch(
            "custom_components.blueprints_updater.__init__.translation.async_get_translations"
        ) as mock_trans,
        patch("custom_components.blueprints_updater.__init__.hashlib", wraps=hashlib),
    ):
        mock_er.return_value.async_get.return_value = entity_entry
        mock_trans.return_value = {
            f"component.{DOMAIN}.exceptions.missing_backup.message": "Translated Missing Backup"
        }

        await async_setup_entry(hass, entry)

        restore_handler = next(
            (
                call.args[2]
                for call in hass.services.async_register.call_args_list
                if call.args[1] == "restore_blueprint"
            ),
            None,
        )

        assert restore_handler is not None

        service_call = ServiceCall(
            hass, DOMAIN, "restore_blueprint", {"entity_id": "update.test", "version": 1}
        )
        result = await restore_handler(service_call)

        assert result["success"] is False
        assert result["message"] == "Translated Missing Backup"
        coordinator_mock.async_restore_blueprint.assert_called_once_with(
            "/config/blueprints/test.yaml", version=1
        )


@pytest.mark.asyncio
async def test_reload_service(hass):
    """Test the reload service handler."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.options = {}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_request_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with patch(
        "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        await async_setup_entry(hass, entry)

        reload_handler = next(
            (
                call.args[2]
                for call in hass.services.async_register.call_args_list
                if call.args[1] == "reload"
            ),
            None,
        )

        assert reload_handler is not None
        await reload_handler(ServiceCall(hass, DOMAIN, "reload", {}))
        coordinator_mock.async_request_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_setup_entry_migration(hass):
    """Test that configuration data is migrated to options."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.data = {"auto_update": True, "update_interval": 12}
    entry.options = {"max_backups": 5}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with patch(
        "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        await async_setup_entry(hass, entry)

        hass.config_entries.async_update_entry.assert_called_once_with(
            entry,
            data={},
            options={"max_backups": 5, "auto_update": True, "update_interval": 12},
        )


@pytest.mark.asyncio
async def test_restore_blueprint_service_errors(hass):
    """Test error scenarios in restore_blueprint service."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.data = {
        "/config/blueprints/test.yaml": {
            "name": "Test",
            "rel_path": "test.yaml",
        }
    }

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch("custom_components.blueprints_updater.__init__.er.async_get") as mock_er,
        patch(
            "custom_components.blueprints_updater.__init__.translation.async_get_translations"
        ) as mock_trans,
    ):
        mock_trans.return_value = {
            f"component.{DOMAIN}.exceptions.missing_entity_id.message": "Missing ID",
            f"component.{DOMAIN}.exceptions.invalid_entity.message": "Invalid Entity",
            f"component.{DOMAIN}.exceptions.not_found.message": "Not Found",
            f"component.{DOMAIN}.exceptions.invalid_version.message": "Invalid Version",
        }

        await async_setup_entry(hass, entry)

        restore_handler = next(
            (
                call.args[2]
                for call in hass.services.async_register.call_args_list
                if call.args[1] == "restore_blueprint"
            ),
            None,
        )

        assert restore_handler is not None

        result = await restore_handler(ServiceCall(hass, DOMAIN, "restore_blueprint", {}))
        assert result["message"] == "Missing ID"

        mock_er.return_value.async_get.return_value = None
        result = await restore_handler(
            ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.none"})
        )
        assert result["message"] == "Invalid Entity"

        bad_entity = MagicMock()
        bad_entity.domain = "switch"
        mock_er.return_value.async_get.return_value = bad_entity
        result = await restore_handler(
            ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "switch.test"})
        )
        assert result["message"] == "Invalid Entity"

        good_entity = MagicMock()
        good_entity.domain = "update"
        good_entity.config_entry_id = entry.entry_id
        good_entity.unique_id = "other_id"
        mock_er.return_value.async_get.return_value = good_entity
        result = await restore_handler(
            ServiceCall(hass, DOMAIN, "restore_blueprint", {"entity_id": "update.test"})
        )
        assert result["message"] == "Not Found"

        good_entity.unique_id = f"blueprint_{hashlib.sha256(b'test.yaml').hexdigest()}"
        result = await restore_handler(
            ServiceCall(
                hass, DOMAIN, "restore_blueprint", {"entity_id": "update.test", "version": 0}
            )
        )
        assert result["message"] == "Invalid Version"


@pytest.mark.asyncio
async def test_unload_entry(hass):
    """Test unloading the entry removes services."""
    entry = MagicMock()
    entry.entry_id = "test_123"
    entry.options = {}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    coordinator_mock = MagicMock()
    coordinator_mock.config_entry = entry
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()

    hass.data = {DOMAIN: {entry.entry_id: coordinator_mock}}

    with patch(
        "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        await async_setup_entry(hass, entry)

        hass.services.async_remove = MagicMock()

        await async_unload_entry(hass, entry)

        assert entry.entry_id not in hass.data[DOMAIN]
        for service in ["reload", "restore_blueprint", "update_all"]:
            hass.services.async_remove.assert_any_call(DOMAIN, service)
