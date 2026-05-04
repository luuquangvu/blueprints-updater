"""Test the initialization of the integration."""

from pathlib import Path

import httpx
import respx
from homeassistant.components.update import SERVICE_INSTALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@respx.mock
async def test_setup_integration(hass: HomeAssistant) -> None:
    """Test setting up the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_entry",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert "coordinators" in hass.data[DOMAIN]
    assert entry.entry_id in hass.data[DOMAIN]["coordinators"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]["coordinators"]


@respx.mock
async def test_full_update_lifecycle(hass: HomeAssistant, respx_mock) -> None:
    """Test the full lifecycle from discovery to update via entity service."""
    blueprints_dir = Path(hass.config.path("blueprints"))
    rel_path = "automation/lifecycle.yaml"
    bp_path = blueprints_dir / rel_path
    bp_path.parent.mkdir(parents=True, exist_ok=True)

    content = "blueprint:\n  name: Life\n  domain: automation\n  source_url: https://example.com/life.yaml\n"
    bp_path.write_text(content, encoding="utf-8")

    new_content = "blueprint:\n  name: Life Updated\n  domain: automation\n  source_url: https://example.com/life.yaml\n"
    respx_mock.get("https://example.com/life.yaml").mock(
        return_value=httpx.Response(200, content=new_content, headers={"Content-Type": "text/yaml"})
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "use_cdn": False},
        entry_id="lifecycle_entry",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    if coordinator._background_task:
        await coordinator._background_task
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, rel_path)
    entity_id = ent_reg.async_get_entity_id("update", DOMAIN, unique_id)

    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"

    await hass.services.async_call(
        "update",
        SERVICE_INSTALL,
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "Life Updated" in Path(bp_path).read_text(encoding="utf-8")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
