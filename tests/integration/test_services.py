"""Test the services provided by Blueprints Updater."""

from pathlib import Path
from unittest.mock import patch

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


def _create_blueprint(hass: HomeAssistant, relative_path: str, content: str) -> str:
    """Helper to create a blueprint file in the HA config directory."""
    blueprints_dir = Path(hass.config.path("blueprints"))
    full_path = blueprints_dir / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return str(full_path)


async def test_reload_service(hass: HomeAssistant) -> None:
    """Test the reload service."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_service_entry",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
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
        options={"update_interval": 24, "filter_mode": "all", "use_cdn": False},
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


async def test_restore_blueprint_service(hass: HomeAssistant, respx_mock) -> None:
    """Test the restore_blueprint service."""
    relative_path = "automation/restore.yaml"
    content = "blueprint:\n  name: Original\n  domain: automation\n  source_url: https://example.com/bp.yaml\n"
    bp_path = _create_blueprint(hass, relative_path, content)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "max_backups": 5, "use_cdn": False},
        entry_id="test_restore",
    )
    entry.add_to_hass(hass)
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

    response = await hass.services.async_call(
        DOMAIN,
        "restore_blueprint",
        {"entity_id": entity_id, "version": 1},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert response.get("success") is True

    restored_content = Path(bp_path).read_text(encoding="utf-8")
    assert "Original" in restored_content

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
