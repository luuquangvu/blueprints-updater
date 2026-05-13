"""Tests for update platform coverage."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import ALLOWED_RELOAD_DOMAINS, DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.update import (
    DOMAIN_AUTOMATION,
    BlueprintUpdateEntity,
    async_setup_entry,
)


@pytest.mark.asyncio
async def test_async_setup_entry_update(hass):
    """Test setting up the update platform."""
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"

    coordinator = MagicMock()
    coordinator._normalize_domain = lambda d: (
        d if d in ALLOWED_RELOAD_DOMAINS else DOMAIN_AUTOMATION
    )
    data = {
        "automation/test.yaml": {
            "name": "Test BP",
            "domain": DOMAIN_AUTOMATION,
            "relative_path": "automation/test.yaml",
            "updatable": True,
            "curr_version": "1.0",
            "remote_version": "1.1",
            "local_hash": "hash12345678",
            "remote_hash": "remot12345678",
        }
    }
    coordinator.data = data

    hass.data = {DOMAIN: {"coordinators": {"test_entry": coordinator}}}

    async_add_entities = MagicMock()

    with patch("custom_components.blueprints_updater.update.async_update_entities") as mock_update:
        await async_setup_entry(hass, config_entry, async_add_entities)
        mock_update.assert_called()


def test_update_entity_properties():
    """Test properties of BlueprintUpdateEntity."""
    coordinator = MagicMock()
    coordinator._normalize_domain = lambda d: (
        d if d in ALLOWED_RELOAD_DOMAINS else DOMAIN_AUTOMATION
    )
    coordinator.config_entry.entry_id = "test_entry"
    info = {
        "name": "Test BP",
        "domain": DOMAIN_AUTOMATION,
        "relative_path": "automation/test.yaml",
        "updatable": True,
        "curr_version": "1.0",
        "remote_version": "1.1",
        "source_url": "https://github.com/test",
        "local_hash": "hash12345678",
        "remote_hash": "remot12345678",
    }
    coordinator.data = {"automation/test.yaml": info}

    entity = BlueprintUpdateEntity(coordinator, "automation/test.yaml", info)

    assert entity.name == "Test BP"
    expected_id = BlueprintUpdateCoordinator.generate_unique_id(
        "test_entry", "automation/test.yaml"
    )
    assert entity.unique_id == expected_id
    assert entity.installed_version == "hash1234"
    assert entity.latest_version == "remot123"

    attrs = entity.extra_state_attributes
    assert attrs["domain"] == DOMAIN_AUTOMATION
    assert attrs["relative_path"] == "automation/test.yaml"
