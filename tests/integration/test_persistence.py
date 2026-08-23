"""Test persistence across real Home Assistant entry restarts."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
    FunctionalDomain,
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

    assert not hass.services.has_service(FunctionalDomain.AUTOMATION, "reload")
    unreloaded = await coordinator.async_reconcile_reload_services([FunctionalDomain.AUTOMATION])

    assert unreloaded == {FunctionalDomain.AUTOMATION}
    assert coordinator._persisted_pending_reload_domains == {FunctionalDomain.AUTOMATION}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    reload_called = asyncio.Event()

    async def _handle_reload(_: ServiceCall) -> None:
        """Record the persisted reload retry."""
        reload_called.set()

    hass.services.async_register(FunctionalDomain.AUTOMATION, "reload", _handle_reload)
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
    hass.services.async_remove(FunctionalDomain.AUTOMATION, "reload")


async def _setup_unmocked_store_coordinator(
    hass: HomeAssistant,
    relative_path: str,
    content: str,
    entry_id: str,
) -> tuple[MockConfigEntry, Path]:
    """Helper to set up a MockConfigEntry and blueprint file with HA's real Store."""
    bp_path = Path(hass.config.path("blueprints")) / relative_path
    bp_path.parent.mkdir(parents=True, exist_ok=True)
    bp_path.write_text(content, encoding="utf-8")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return entry, bp_path


@pytest.mark.asyncio
async def test_unmocked_storage_persistence_lifecycle(hass: HomeAssistant) -> None:
    """Ensure metadata survives a Store round-trip and validators are cleared on mismatch."""
    from homeassistant.helpers.storage import Store

    relative_path = "automation/unmocked_store.yaml"
    content = (
        "blueprint:\n"
        "  name: Unmocked Store\n"
        "  domain: automation\n"
        "  source_url: https://example.com/unmocked.yaml\n"
    )
    entry, bp_path = await _setup_unmocked_store_coordinator(
        hass, relative_path, content, "unmocked_store_entry"
    )

    with (
        patch("custom_components.blueprints_updater.coordinator.Store", side_effect=Store),
        patch(
            "custom_components.blueprints_updater.coordinator."
            "BlueprintUpdateCoordinator._async_update_blueprint_in_place",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
        await coordinator.async_wait_until_done()

        bp_key = str(bp_path)
        assert bp_key in coordinator.data

        # Seed simulated non-None remote values on the matching coordinator.data entry
        coordinator.data[bp_key].update(
            {
                "remote_hash": "a1b2c3d4e5f6",
                "etag": '"etag-12345"',
                "last_modified": "Fri, 07 Aug 2026 12:00:00 GMT",
                "source_url": "https://example.com/unmocked.yaml",
            }
        )

        await coordinator._async_save_metadata()

        # Assert persisted metadata contains the non-None values before unloading
        assert relative_path in coordinator._persisted_metadata
        pre_unload_saved = coordinator._persisted_metadata[relative_path]
        assert pre_unload_saved["etag"] == '"etag-12345"'
        assert pre_unload_saved["remote_hash"] == "a1b2c3d4e5f6"
        assert pre_unload_saved["last_modified"] == "Fri, 07 Aug 2026 12:00:00 GMT"

        # Simulate entry restart (unload + setup)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reloaded_coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
        await reloaded_coordinator.async_wait_until_done()

        # Verify metadata survived Store round-trip and validators are cleared on hash mismatch
        assert relative_path in reloaded_coordinator._persisted_metadata
        restarted_saved = reloaded_coordinator._persisted_metadata[relative_path]

        assert restarted_saved["etag"] is None
        assert restarted_saved["last_modified"] is None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
