"""Test the services provided by Blueprints Updater."""

import inspect
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from tests.compat import async_call_service_compat


async def _call_restore_for_failure(hass: HomeAssistant, entity_id: str) -> None:
    """Call restore using the response contract supported by this HA version."""
    await async_call_service_compat(
        hass,
        DOMAIN,
        "restore_blueprint",
        {"entity_id": entity_id, "version": 1},
        return_response=True,
    )


@pytest.mark.asyncio
async def test_reload_service(hass: HomeAssistant, setup_integration_entry) -> None:
    """Test the reload service."""
    async with setup_integration_entry(entry_id="test_service_entry") as entry:
        coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]

        with patch.object(coordinator, "async_request_refresh") as mock_refresh:
            await hass.services.async_call(
                DOMAIN,
                "reload",
                {},
                blocking=True,
            )
            mock_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_update_all_service(
    hass: HomeAssistant,
    create_blueprint: Callable[[str, str], str],
    respx_mock,
) -> None:
    """Test the update_all service."""
    content = "blueprint:\n  name: Test\n  domain: automation\n  source_url: https://raw.githubusercontent.com/user/repo/main/test.yaml\n"
    create_blueprint("automation/test.yaml", content)

    new_content = "blueprint:\n  name: Test Updated\n  domain: automation\n  source_url: https://raw.githubusercontent.com/user/repo/main/test.yaml\n"
    respx_mock.get("https://raw.githubusercontent.com/user/repo/main/test.yaml").mock(
        return_value=httpx.Response(
            HTTPStatus.OK, content=new_content, headers={"Content-Type": "text/yaml"}
        )
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "filter_mode": "all"},
        entry_id="test_update_all",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    await coordinator.async_wait_until_done()

    blueprint_path = str(Path(hass.config.path("blueprints")) / "automation/test.yaml")
    assert coordinator.data[blueprint_path]["updatable"] is True

    await hass.services.async_call(
        DOMAIN,
        "update_all",
        {"backup": False},
        blocking=True,
    )
    await hass.async_block_till_done()

    updated_content = Path(blueprint_path).read_text(encoding="utf-8")
    assert "Test Updated" in updated_content
    assert coordinator.data[blueprint_path]["updatable"] is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_restore_blueprint_service(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    create_blueprint: Callable[[str, str], str],
    setup_integration_entry,
) -> None:
    """Test the restore_blueprint service."""
    relative_path = "automation/restore.yaml"
    content = "blueprint:\n  name: Original\n  domain: automation\n  source_url: https://example.com/bp.yaml\n"
    bp_path = create_blueprint(relative_path, content)

    async with setup_integration_entry(
        entry_id="test_restore",
        options={"update_interval": 24, "max_backups": 5},
    ) as entry:
        coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]

        await coordinator.async_install_blueprint(bp_path, content, backup=True)

        Path(bp_path).write_text("CORRUPTED", encoding="utf-8")

        unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
        await coordinator.async_wait_until_done()

        entity_id = entity_registry.async_get_entity_id("update", DOMAIN, unique_id)
        assert entity_id is not None

        response = await async_call_service_compat(
            hass,
            DOMAIN,
            "restore_blueprint",
            {"entity_id": entity_id, "version": 1},
            return_response=True,
        )
        if "supports_response" in inspect.signature(async_register_admin_service).parameters:
            assert response is not None
            assert response.get("success") is True

        restored_content = Path(bp_path).read_text(encoding="utf-8")
        assert "Original" in restored_content


@pytest.mark.asyncio
async def test_restore_service_reports_missing_backup(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    create_blueprint: Callable[[str, str], str],
    setup_integration_entry,
) -> None:
    """Test that restore failures cross the real service boundary safely."""
    relative_path = "automation/no-backup.yaml"
    content = (
        "blueprint:\n"
        "  name: No Backup\n"
        "  domain: automation\n"
        "  source_url: https://example.com/no-backup.yaml\n"
    )
    create_blueprint(relative_path, content)

    async with setup_integration_entry(
        entry_id="missing_backup",
        options={"update_interval": 24, "max_backups": 5},
    ) as entry:
        unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
        entity_id = entity_registry.async_get_entity_id("update", DOMAIN, unique_id)
        assert entity_id is not None

        with pytest.raises(ServiceValidationError) as exc_info:
            await _call_restore_for_failure(hass, entity_id)

        assert exc_info.value.translation_key == "missing_backup"
        assert exc_info.value.translation_domain == DOMAIN


@pytest.mark.asyncio
async def test_restore_service_translates_preparation_revision_mismatch(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    create_blueprint: Callable[[str, str], str],
    setup_integration_entry,
) -> None:
    """Test actionable handling when the target changes before restore preparation."""
    relative_path = "automation/revision-mismatch.yaml"
    content = (
        "blueprint:\n"
        "  name: Revision Mismatch\n"
        "  domain: automation\n"
        "  source_url: https://example.com/revision-mismatch.yaml\n"
    )
    bp_path = Path(create_blueprint(relative_path, content))
    Path(f"{bp_path}.bak.1").write_text(content, encoding="utf-8")

    async with setup_integration_entry(
        entry_id="revision_mismatch",
        options={"update_interval": 24, "max_backups": 5},
    ) as entry:
        unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
        entity_id = entity_registry.async_get_entity_id("update", DOMAIN, unique_id)
        assert entity_id is not None

        bp_path.unlink()
        bp_path.mkdir()

        try:
            with pytest.raises(
                ServiceValidationError,
                match="Local blueprint changed; refresh and retry the update",
            ):
                await _call_restore_for_failure(hass, entity_id)
        finally:
            bp_path.rmdir()
            bp_path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_async_purge_entity_registry_removes_registry_and_state(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test _async_purge_entity_registry removes entity entry and state machine state."""
    from custom_components.blueprints_updater.update import _async_purge_entity_registry

    entry = entity_registry.async_get_or_create(
        domain="update",
        platform=DOMAIN,
        unique_id="test_purge_unique_id",
        suggested_object_id="test_purge_entity",
    )
    entity_id = entry.entity_id

    hass.states.async_set(entity_id, "on", {"friendly_name": "Purge Test"})
    assert hass.states.get(entity_id) is not None
    assert entity_registry.async_get(entity_id) is not None

    await _async_purge_entity_registry(hass, entity_registry, entity_id)

    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None
