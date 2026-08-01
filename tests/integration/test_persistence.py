"""Test persistence across real Home Assistant entry restarts."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
    DOMAIN_AUTOMATION,
)


@pytest.mark.asyncio
async def test_pending_reload_persists_and_retries_after_restart(hass: HomeAssistant) -> None:
    """Test durable pending reload state with Home Assistant's real Store."""
    relative_path = "automation/persisted.yaml"
    bp_path = Path(hass.config.path("blueprints")) / relative_path
    bp_path.parent.mkdir(parents=True, exist_ok=True)
    bp_path.write_text(
        "blueprint:\n"
        "  name: Persisted\n"
        "  domain: automation\n"
        "  source_url: https://example.com/persisted.yaml\n",
        encoding="utf-8",
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="persistence_entry",
    )
    entry.add_to_hass(hass)

    remote_refresh = (
        "custom_components.blueprints_updater.coordinator."
        "BlueprintUpdateCoordinator._async_update_blueprint_in_place"
    )
    with patch(remote_refresh, new_callable=AsyncMock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
        await coordinator.async_wait_until_done()

    assert not hass.services.has_service(DOMAIN_AUTOMATION, "reload")
    unreloaded = await coordinator.async_reconcile_reload_services([DOMAIN_AUTOMATION])

    assert unreloaded == {DOMAIN_AUTOMATION}
    assert coordinator._persisted_pending_reload_domains == {DOMAIN_AUTOMATION}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    reload_called = asyncio.Event()

    async def _handle_reload(_: ServiceCall) -> None:
        """Record the persisted reload retry."""
        reload_called.set()

    hass.services.async_register(DOMAIN_AUTOMATION, "reload", _handle_reload)
    with patch(remote_refresh, new_callable=AsyncMock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        restarted = hass.data[DOMAIN]["coordinators"][entry.entry_id]
        await restarted.async_wait_until_done()

    assert reload_called.is_set()
    assert restarted._pending_reload_domains == set()
    assert restarted._persisted_pending_reload_domains == set()
    assert relative_path in restarted._persisted_metadata
    assert restarted.data[str(bp_path)]["reload_pending"] is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.services.async_remove(DOMAIN_AUTOMATION, "reload")
