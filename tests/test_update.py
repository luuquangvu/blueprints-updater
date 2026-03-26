from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.update import BlueprintUpdateEntity, async_setup_entry


@pytest.mark.asyncio
async def test_update_entities_lifecycle(hass):
    """Test that entities are added and removed correctly."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()

    coordinator = MagicMock()
    coordinator.data = {
        "/config/blueprints/test1.yaml": {
            "name": "Test 1",
            "rel_path": "test1.yaml",
            "source_url": "https://url1.com",
            "local_hash": "hash1",
        }
    }
    coordinator.async_add_listener = MagicMock()

    hass.data = {DOMAIN: {"test_entry": coordinator}}
    hass.async_create_task = MagicMock()
    hass.states = MagicMock()

    async_add_entities = MagicMock()

    mock_entity_registry = MagicMock()

    with patch(
        "custom_components.blueprints_updater.update.er.async_get",
        return_value=mock_entity_registry,
    ):
        await async_setup_entry(hass, entry, async_add_entities)

        assert async_add_entities.called
        added_entities = async_add_entities.call_args[0][0]
        assert len(added_entities) == 1
        entity = added_entities[0]
        assert entity._path == "/config/blueprints/test1.yaml"

        update_callback = coordinator.async_add_listener.call_args[0][0]

        coordinator.data["/config/blueprints/test2.yaml"] = {
            "name": "Test 2",
            "rel_path": "test2.yaml",
            "source_url": "https://url2.com",
            "local_hash": "hash2",
        }

        async_add_entities.reset_mock()
        update_callback()

        assert async_add_entities.called
        added_entities = async_add_entities.call_args[0][0]
        assert len(added_entities) == 1
        entity2 = added_entities[0]
        assert entity2._path == "/config/blueprints/test2.yaml"

        del coordinator.data["/config/blueprints/test1.yaml"]
        entity.entity_id = "update.test1"
        mock_entity_registry.async_get.return_value = MagicMock()

        update_callback()

        mock_entity_registry.async_remove.assert_called_once_with("update.test1")


@pytest.fixture
def coordinator():
    """Fixture for BlueprintUpdateCoordinator in update tests."""
    comp = MagicMock()
    comp.data = {
        "/config/blueprints/test.yaml": {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": "https://url.com",
            "local_hash": "hash1xxxxxxxxxxx",
            "remote_hash": "hash2xxxxxxxxxxx",
            "updatable": True,
            "last_error": None,
            "remote_content": "blueprint:\n  name: Test",
        }
    }
    comp.config_entry = MagicMock()
    comp.config_entry.options = {"auto_update": True}
    comp.async_install_blueprint = AsyncMock()
    comp.async_refresh = AsyncMock()
    return comp


def test_entity_properties(coordinator):
    """Test properties of BlueprintUpdateEntity."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )

    assert entity.name == "Test"
    assert entity._path == "/config/blueprints/test.yaml"
    assert entity.auto_update is True
    assert entity.installed_version == "hash1xxx"
    assert entity.latest_version == "hash2xxx"
    assert entity.release_summary == (
        "Update available from https://url.com\n\n"
        "Warning: Auto-update may carry backward incompatibility risks "
        "if the author introduces breaking changes."
    )
    assert entity.extra_state_attributes == {}

    entity_missing = BlueprintUpdateEntity(
        coordinator, "/missing.yaml", {"name": "Missing", "rel_path": "missing"}
    )
    assert entity_missing.installed_version is None
    assert entity_missing.latest_version is None
    assert entity_missing.release_summary is None

    coordinator.data["/config/blueprints/test.yaml"]["last_error"] = "Fetch Error"
    assert entity.extra_state_attributes == {"last_error": "Fetch Error"}


@pytest.mark.asyncio
async def test_entity_async_install(coordinator):
    """Test async_install method of BlueprintUpdateEntity."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )

    await entity.async_install(version=None, backup=False)
    coordinator.async_install_blueprint.assert_called_once_with(
        "/config/blueprints/test.yaml",
        "blueprint:\n  name: Test",
        reload_services=True,
        backup=False,
    )
    coordinator.async_refresh.assert_called_once()

    coordinator.data.pop("/config/blueprints/test.yaml")
    await entity.async_install(version=None, backup=False)

    coordinator.data["/config/blueprints/test.yaml"] = {"last_error": "Syntax Error"}

    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="Cannot install blueprint: Syntax Error"):
        await entity.async_install(version=None, backup=False)


@pytest.mark.asyncio
async def test_entity_async_install_backup(coordinator):
    """Test async_install method with backup enabled."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )

    await entity.async_install(version=None, backup=True)
    coordinator.async_install_blueprint.assert_called_once_with(
        "/config/blueprints/test.yaml",
        "blueprint:\n  name: Test",
        reload_services=True,
        backup=True,
    )
    coordinator.async_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_entity_release_summary_with_usage(coordinator):
    """Test release summary includes usage warning."""
    import custom_components.blueprints_updater.update as update_module

    # Test automation usage
    info_auto = {
        "name": "Test Auto",
        "rel_path": "automation/test.yaml",
        "source_url": "https://url.com",
        "updatable": True,
    }
    entity_auto = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/automation/test.yaml",
        info_auto,
    )
    coordinator.data["/config/blueprints/automation/test.yaml"] = info_auto

    with patch.object(update_module, "automations_with_blueprint", return_value=["auto1", "auto2"]):
        summary = entity_auto.release_summary
        assert summary is not None
        assert "affect 2 running automation(s)" in summary

    info_script = {
        "name": "Test Script",
        "rel_path": "script/test2.yaml",
        "source_url": "https://url.com",
        "updatable": True,
    }
    entity_script = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/script/test2.yaml",
        info_script,
    )
    coordinator.data["/config/blueprints/script/test2.yaml"] = info_script

    with patch.object(update_module, "scripts_with_blueprint", return_value=["script1"]):
        summary = entity_script.release_summary
        assert summary is not None
        assert "affect 1 running script(s)" in summary


@pytest.mark.asyncio
async def test_entity_skip_version(coordinator):
    """Test that skipping a version works natively."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "blueprint_with_update.yaml",
        {
            "name": "Update Blueprint",
            "rel_path": "blueprint_with_update.yaml",
        },
    )

    coordinator.data = {
        "blueprint_with_update.yaml": {
            "local_hash": "hash1xxxxxxxxxxxx",
            "remote_hash": "hash2xxxxxxxxxxxx",
            "updatable": True,
            "source_url": "https://url.com",
        }
    }

    entity.hass = coordinator.hass

    with patch.object(entity, "async_write_ha_state"):
        assert entity.state == "on"

        await entity.async_skip()

        assert entity.state == "off"

        attrs = entity.state_attributes
        assert attrs is not None
        assert attrs["skipped_version"] == "hash2xxx"

        await entity.async_clear_skipped()
        assert entity.state == "on"
