"""Test the initialization of the integration."""

import socket
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from homeassistant.components.update import SERVICE_INSTALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
    FunctionalDomain,
    IntegrationService,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.mark.asyncio
async def test_setup_integration(hass: HomeAssistant) -> None:
    """Test setting up the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_entry",
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

    assert DOMAIN in hass.data
    assert "coordinators" in hass.data[DOMAIN]
    assert entry.entry_id in hass.data[DOMAIN]["coordinators"]
    for service in IntegrationService:
        assert hass.services.has_service(DOMAIN, service)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]["coordinators"]
    for service in IntegrationService:
        assert not hass.services.has_service(DOMAIN, service)


@pytest.mark.parametrize(
    "blueprint_domain",
    [FunctionalDomain.AUTOMATION, FunctionalDomain.SCRIPT, FunctionalDomain.TEMPLATE],
)
@pytest.mark.asyncio
async def test_full_update_lifecycle(
    hass: HomeAssistant,
    respx_mock,
    blueprint_domain: str,
) -> None:
    """Test the full lifecycle from discovery to update via entity service."""
    blueprints_dir = Path(hass.config.path("blueprints"))
    relative_path = f"{blueprint_domain}/lifecycle.yaml"
    bp_path = blueprints_dir / relative_path
    bp_path.parent.mkdir(parents=True, exist_ok=True)

    source_url = f"https://example.com/{blueprint_domain}-life.yaml"
    content = (
        f"blueprint:\n  name: Life\n  domain: {blueprint_domain}\n  source_url: {source_url}\n"
    )
    bp_path.write_text(content, encoding="utf-8")

    new_content = (
        "blueprint:\n"
        "  name: Life Updated\n"
        f"  domain: {blueprint_domain}\n"
        f"  source_url: {source_url}\n"
    )
    respx_mock.get(source_url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK, content=new_content, headers={"Content-Type": "text/yaml"}
        )
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="lifecycle_entry",
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

    ent_reg = er.async_get(hass)

    unique_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, relative_path)
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


@pytest.mark.asyncio
async def test_auto_update_lifecycle(hass: HomeAssistant, respx_mock) -> None:
    """Test that setup can discover and automatically install an update."""
    relative_path = "automation/auto-update.yaml"
    bp_path = Path(hass.config.path("blueprints")) / relative_path
    bp_path.parent.mkdir(parents=True, exist_ok=True)
    source_url = "https://example.com/auto-update.yaml"
    bp_path.write_text(
        f"blueprint:\n  name: Auto\n  domain: automation\n  source_url: {source_url}\n",
        encoding="utf-8",
    )
    respx_mock.get(source_url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK,
            content=(
                "blueprint:\n"
                "  name: Auto Updated\n"
                "  domain: automation\n"
                f"  source_url: {source_url}\n"
            ),
            headers={"Content-Type": "text/yaml"},
        )
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24, "auto_update": True},
        entry_id="auto_update_entry",
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

    assert "Auto Updated" in bp_path.read_text(encoding="utf-8")
    assert coordinator.data[str(bp_path)]["updatable"] is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_config_migration_and_options_update(hass: HomeAssistant) -> None:
    """Test legacy data migration and the real options-listener lifecycle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"update_interval": 12},
        options={"max_backups": 3},
        entry_id="migration_entry",
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
        assert entry.data == {}
        assert entry.options["update_interval"] == 12
        assert entry.options["max_backups"] == 3

        with patch.object(
            coordinator,
            "async_request_refresh",
            new_callable=AsyncMock,
        ) as request_refresh:
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, "update_interval": 6},
            )
            await hass.async_block_till_done()

        request_refresh.assert_awaited_once()
        assert coordinator.update_interval == timedelta(hours=6)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_setup_failure_rollback_cleans_up_coordinator(hass: HomeAssistant) -> None:
    """Test that platform setup failure rolls back coordinator registration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="failed_setup_entry",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            side_effect=RuntimeError("Platform setup failed"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinators = hass.data.get(DOMAIN, {}).get("coordinators", {})
    assert entry.entry_id not in coordinators
