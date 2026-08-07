"""Test the services provided by Blueprints Updater."""

import inspect
import socket
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


def _create_blueprint(hass: HomeAssistant, relative_path: str, content: str) -> str:
    """Helper to create a blueprint file in the HA config directory."""
    blueprints_dir = Path(hass.config.path("blueprints"))
    full_path = blueprints_dir / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return str(full_path)


async def _call_restore_for_failure(hass: HomeAssistant, entity_id: str) -> None:
    """Call restore using the response contract supported by this HA version."""
    if "supports_response" in inspect.signature(async_register_admin_service).parameters:
        await hass.services.async_call(
            DOMAIN,
            "restore_blueprint",
            {"entity_id": entity_id, "version": 1},
            blocking=True,
            return_response=True,
        )
    else:
        await hass.services.async_call(
            DOMAIN,
            "restore_blueprint",
            {"entity_id": entity_id, "version": 1},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_reload_service(hass: HomeAssistant) -> None:
    """Test the reload service."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_service_entry",
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
        ),
        patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]

    with patch.object(coordinator, "async_request_refresh") as mock_refresh:
        await hass.services.async_call(
            DOMAIN,
            "reload",
            {},
            blocking=True,
        )
        mock_refresh.assert_called_once()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_update_all_service(hass: HomeAssistant, respx_mock) -> None:
    """Test the update_all service."""
    content = "blueprint:\n  name: Test\n  domain: automation\n  source_url: https://raw.githubusercontent.com/user/repo/main/test.yaml\n"
    _create_blueprint(hass, "automation/test.yaml", content)

    new_content = "blueprint:\n  name: Test Updated\n  domain: automation\n  source_url: https://raw.githubusercontent.com/user/repo/main/test.yaml\n"
    respx_mock.get("https://raw.githubusercontent.com/user/repo/main/test.yaml").mock(
        return_value=httpx.Response(200, content=new_content, headers={"Content-Type": "text/yaml"})
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "filter_mode": "all"},
        entry_id="test_update_all",
    )
    entry.add_to_hass(hass)
    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
    ):
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
async def test_restore_blueprint_service(hass: HomeAssistant, respx_mock) -> None:
    """Test the restore_blueprint service."""
    relative_path = "automation/restore.yaml"
    content = "blueprint:\n  name: Original\n  domain: automation\n  source_url: https://example.com/bp.yaml\n"
    bp_path = _create_blueprint(hass, relative_path, content)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "max_backups": 5},
        entry_id="test_restore",
    )
    entry.add_to_hass(hass)
    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]

    await coordinator.async_install_blueprint(bp_path, content, backup=True)

    Path(bp_path).write_text("CORRUPTED", encoding="utf-8")

    ent_reg = er.async_get(hass)

    unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
    await coordinator.async_wait_until_done()

    entity_id = ent_reg.async_get_entity_id("update", DOMAIN, unique_id)
    assert entity_id is not None

    admin_svc_sig = inspect.signature(async_register_admin_service)
    supports_response_available = "supports_response" in admin_svc_sig.parameters

    if supports_response_available:
        response = await hass.services.async_call(
            DOMAIN,
            "restore_blueprint",
            {"entity_id": entity_id, "version": 1},
            blocking=True,
            return_response=True,
        )
        assert response is not None
        assert response.get("success") is True
    else:
        await hass.services.async_call(
            DOMAIN,
            "restore_blueprint",
            {"entity_id": entity_id, "version": 1},
            blocking=True,
        )

    restored_content = Path(bp_path).read_text(encoding="utf-8")
    assert "Original" in restored_content

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_restore_service_reports_missing_backup(hass: HomeAssistant) -> None:
    """Test that restore failures cross the real service boundary safely."""
    relative_path = "automation/no-backup.yaml"
    content = (
        "blueprint:\n"
        "  name: No Backup\n"
        "  domain: automation\n"
        "  source_url: https://example.com/no-backup.yaml\n"
    )
    _create_blueprint(hass, relative_path, content)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "max_backups": 5},
        entry_id="missing_backup",
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
        ),
        patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
        entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, unique_id)
        assert entity_id is not None

        with pytest.raises(ServiceValidationError):
            await _call_restore_for_failure(hass, entity_id)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_restore_service_translates_preparation_revision_mismatch(
    hass: HomeAssistant,
) -> None:
    """Test actionable handling when the target changes before restore preparation."""
    relative_path = "automation/revision-mismatch.yaml"
    content = (
        "blueprint:\n"
        "  name: Revision Mismatch\n"
        "  domain: automation\n"
        "  source_url: https://example.com/revision-mismatch.yaml\n"
    )
    bp_path = Path(_create_blueprint(hass, relative_path, content))
    Path(f"{bp_path}.bak.1").write_text(content, encoding="utf-8")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "max_backups": 5},
        entry_id="revision_mismatch",
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
        ),
        patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
        entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, unique_id)
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

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_async_purge_entity_registry_removes_registry_and_state(
    hass: HomeAssistant,
) -> None:
    """Test _async_purge_entity_registry removes entity entry and state machine state."""
    from custom_components.blueprints_updater.update import _async_purge_entity_registry

    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get_or_create(
        domain="update",
        platform=DOMAIN,
        unique_id="test_purge_unique_id",
        suggested_object_id="test_purge_entity",
    )
    entity_id = entry.entity_id

    hass.states.async_set(entity_id, "on", {"friendly_name": "Purge Test"})
    assert hass.states.get(entity_id) is not None
    assert entity_reg.async_get(entity_id) is not None

    await _async_purge_entity_registry(hass, entity_reg, entity_id)

    assert hass.states.get(entity_id) is None
    assert entity_reg.async_get(entity_id) is None
