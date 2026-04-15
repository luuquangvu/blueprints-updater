"""Tests for Blueprints Updater update entities."""

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

import custom_components.blueprints_updater.update as update_module
from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
    GitDiffResult,
)
from custom_components.blueprints_updater.update import BlueprintUpdateEntity, async_setup_entry


async def await_scheduled_update(entity, coordinator):
    """Wait for a scheduled update task to complete."""
    with patch.object(entity, "async_write_ha_state"):
        entity._handle_coordinator_update()
        coro = coordinator.hass.async_create_task.call_args[0][0]
        await coro


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

    hass.data = {DOMAIN: {"coordinators": {"test_entry": coordinator}}}
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
    comp.async_fetch_blueprint = AsyncMock()
    comp.async_refresh = AsyncMock()
    comp.async_translate = AsyncMock(
        side_effect=lambda key, **kwargs: {
            "up_to_date": "Up to date",
            "update_available_short": "Update available",
            "update_available": f"Update available from {kwargs.get('source_url')}",
            "auto_update_warning": (
                "Warning: Updates may carry backward incompatibility risks "
                "if the author introduces breaking changes. It is highly recommended "
                "to enable the backup option before installing."
            ),
            "usage_warning": (
                f"Warning: This update will affect {kwargs.get('count')} "
                f"running {kwargs.get('domain')}(s)."
            ),
            "install_error": (
                f"Cannot install blueprint: {kwargs.get('error')}. "
                "The remote file has errors and cannot be safely applied."
            ),
        }.get(key, key)
    )
    comp.hass = MagicMock()

    async def mock_exec(func, *args):
        """Mock executor task."""
        return func(*args)

    comp.hass.async_add_executor_job = AsyncMock(side_effect=mock_exec)
    comp.get_cached_git_diff = MagicMock(return_value=None)
    comp.set_cached_git_diff = MagicMock()
    comp.async_get_git_diff = AsyncMock(
        return_value=GitDiffResult(diff_text="", is_semantic_sync=False)
    )
    comp.is_auto_update_enabled = MagicMock(
        side_effect=lambda _: comp.config_entry.options.get(
            "auto_update", comp.config_entry.data.get("auto_update", True)
        )
    )
    return comp


@pytest.mark.asyncio
async def test_entity_properties(coordinator):
    """Test properties of BlueprintUpdateEntity."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )
    entity.hass = coordinator.hass
    entity.entity_id = "update.test"
    with patch.object(entity, "async_write_ha_state"):
        await entity._async_localize_strings()

    assert entity.name == "Test"
    assert entity.release_url == "https://url.com"
    assert entity._path == "/config/blueprints/test.yaml"
    assert entity.auto_update is True
    assert entity.installed_version == "hash1xxx"
    assert entity.latest_version == "hash2xxx"
    assert entity.release_summary == "Update available"
    assert await entity.async_release_notes() == (
        "Update available from https://url.com\n\n"
        "Warning: Updates may carry backward incompatibility risks "
        "if the author introduces breaking changes. It is highly recommended "
        "to enable the backup option before installing."
    )
    assert entity.extra_state_attributes == {}

    entity_missing = BlueprintUpdateEntity(
        coordinator,
        "/missing.yaml",
        {"name": "Missing", "rel_path": "missing", "source_url": "https://url.com"},
    )
    entity_missing.hass = coordinator.hass
    entity_missing.entity_id = "update.missing"
    with patch.object(entity_missing, "async_write_ha_state"):
        await entity_missing._async_localize_strings()
    assert entity_missing.installed_version is None
    assert entity_missing.latest_version is None
    assert entity_missing.release_summary is None
    assert await entity_missing.async_release_notes() is None

    coordinator.data["/config/blueprints/test.yaml"]["last_error"] = "Fetch Error"
    await await_scheduled_update(entity, coordinator)
    assert entity.extra_state_attributes == {"last_error": "Fetch Error"}

    path = "/config/blueprints/test.yaml"
    old_local_hash = coordinator.data[path]["local_hash"]
    old_remote_hash = coordinator.data[path]["remote_hash"]

    assert entity.installed_version == old_local_hash[:8]
    assert entity.latest_version == old_remote_hash[:8]

    coordinator.data[path]["local_hash"] = "newlocal"
    coordinator.data[path]["remote_hash"] = "newremote"
    await await_scheduled_update(entity, coordinator)
    assert entity.installed_version == "newlocal"[:8]
    assert entity.latest_version == "newremote"[:8]


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
    entity.hass = coordinator.hass

    with pytest.raises(HomeAssistantError, match="Cannot install blueprint: Syntax Error"):
        await entity.async_install(version=None, backup=False)


@pytest.mark.asyncio
async def test_entity_async_install_on_demand_fetch(coordinator):
    """Test async_install triggers on-demand fetch if content is missing."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )

    coordinator.data["/config/blueprints/test.yaml"]["remote_content"] = None
    coordinator.data["/config/blueprints/test.yaml"]["updatable"] = True

    async def mock_fetch(path: str, force: bool = False) -> None:
        """Mock fetch blueprint content and update coordinator data."""
        _ = force
        coordinator.data[path].update({"remote_content": "fetched content"})

    coordinator.async_fetch_blueprint = AsyncMock(side_effect=mock_fetch)

    await entity.async_install(version=None, backup=False)

    coordinator.async_fetch_blueprint.assert_called_once_with(
        "/config/blueprints/test.yaml", force=True
    )
    coordinator.async_install_blueprint.assert_called_once_with(
        "/config/blueprints/test.yaml",
        "fetched content",
        reload_services=True,
        backup=False,
    )


@pytest.mark.asyncio
async def test_entity_async_install_content_missing_fail(coordinator):
    """Test async_install fails if content is still missing after fetch."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )

    coordinator.data["/config/blueprints/test.yaml"]["remote_content"] = None
    coordinator.async_fetch_blueprint = AsyncMock()

    with pytest.raises(HomeAssistantError, match="Cannot install blueprint: content_missing"):
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
    entity_auto.entity_id = "update.auto"
    coordinator.data["/config/blueprints/automation/test.yaml"] = info_auto
    entity_auto.hass = coordinator.hass

    with (
        patch.object(update_module, "automations_with_blueprint", return_value=["auto1", "auto2"]),
        patch.object(entity_auto, "async_write_ha_state"),
    ):
        await entity_auto._async_localize_strings()
        assert entity_auto.release_summary == "Update available"
        notes = await entity_auto.async_release_notes()
        assert notes is not None
        assert "affect 2 running automation(s)" in notes

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
    entity_script.entity_id = "update.script"
    coordinator.data["/config/blueprints/script/test2.yaml"] = info_script
    entity_script.hass = coordinator.hass

    with (
        patch.object(update_module, "scripts_with_blueprint", return_value=["script1"]),
        patch.object(entity_script, "async_write_ha_state"),
    ):
        await entity_script._async_localize_strings()
        assert entity_script.release_summary == "Update available"
        notes = await entity_script.async_release_notes()
        assert notes is not None
        assert "affect 1 running script(s)" in notes


@pytest.mark.asyncio
async def test_entity_release_notes_usage_error_handled(coordinator):
    """Test that HomeAssistantError in usage calculation is handled."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "updatable": True,
        "remote_content": "",
    }
    coordinator.data[path] = info
    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    with (
        patch.object(update_module, "automations_with_blueprint", side_effect=HomeAssistantError),
        patch("custom_components.blueprints_updater.update._LOGGER") as mock_logger,
    ):
        notes = await entity.async_generate_release_notes()
        assert notes is not None
        assert "affect" not in notes
        mock_logger.warning.assert_called()
        _, kwargs = mock_logger.warning.call_args
        assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_entity_release_notes_usage_error_unhandled(coordinator):
    """Test that TypeError in usage calculation is NOT handled."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "updatable": True,
        "remote_content": "",
    }
    coordinator.data[path] = info
    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    with (
        patch.object(update_module, "automations_with_blueprint", side_effect=TypeError),
        pytest.raises(TypeError),
    ):
        await entity.async_generate_release_notes()


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


@pytest.mark.asyncio
async def test_async_update_entities_migration(hass):
    """Validate legacy rel_path IDs are migrated and unknown entities removed."""
    entry = MagicMock()
    entry.entry_id = "test_entry"

    coordinator = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator.config_entry = entry
    coordinator.data = {
        "/config/blueprints/kept.yaml": {
            "rel_path": "kept.yaml",
            "name": "Kept",
            "local_hash": "hash",
        },
    }
    coordinator.async_add_listener = MagicMock()

    hass.data = {DOMAIN: {"coordinators": {entry.entry_id: coordinator}}}
    hass.states = MagicMock()

    mock_entity_registry = MagicMock()

    legacy_id = BlueprintUpdateCoordinator.generate_legacy_unique_id("kept.yaml")
    new_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, "kept.yaml")

    legacy_entity = MagicMock()
    legacy_entity.domain = "update"
    legacy_entity.unique_id = legacy_id
    legacy_entity.entity_id = "update.kept"

    orphan_entity = MagicMock()
    orphan_entity.domain = "update"
    orphan_entity.unique_id = "test_entry_orphan.yaml"
    orphan_entity.entity_id = "update.orphan"

    with (
        patch(
            "custom_components.blueprints_updater.update.er.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.blueprints_updater.update.er.async_entries_for_config_entry",
            return_value=[legacy_entity, orphan_entity],
        ),
    ):
        async_add_entities = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

        mock_entity_registry.async_update_entity.assert_called_once_with(
            "update.kept", new_unique_id=new_id
        )
        mock_entity_registry.async_remove.assert_called_once_with("update.orphan")
        hass.states.async_remove.assert_called_once_with("update.orphan")


@pytest.mark.asyncio
async def test_entity_auto_update_cache_invalidation(coordinator):
    """Test that auto_update property is correctly invalidated and refreshed."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )
    entity.hass = coordinator.hass
    entity.entity_id = "update.test"

    assert entity.auto_update is True

    coordinator.config_entry.options = {"auto_update": False}

    await await_scheduled_update(entity, coordinator)

    assert entity.auto_update is False


@pytest.mark.asyncio
async def test_entity_availability_behavior(coordinator):
    """Test that entity availability correctly follows coordinator state."""
    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/test.yaml",
        coordinator.data["/config/blueprints/test.yaml"],
    )
    entity.hass = coordinator.hass

    coordinator.last_update_success = True
    assert entity.available is True
    coordinator.last_update_success = False
    assert entity.available is False
    coordinator.last_update_success = True
    assert entity.available is True


@pytest.mark.asyncio
async def test_entity_release_notes_git_diff(coordinator):
    """Test git diff generation in release notes."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }

    coordinator.data[path] = info
    info["remote_content"] = (
        "blueprint:\n  name: Test\n  source_url: https://url.com\n"
        "  domain: automation\naction: []\n"
    )
    local_content = (
        "blueprint:\n  name: Test\n  source_url: https://url.com\n"
        "  domain: automation\ncondition: []\naction: []\n"
    )

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff.return_value = GitDiffResult(
        diff_text="-condition: []", is_semantic_sync=False
    )
    with patch("builtins.open", mock_open(read_data=local_content)):
        notes = await entity.async_generate_release_notes()

    assert notes is not None
    assert "```diff" in notes
    assert "-condition: []" in notes
    assert "```" in notes
    assert "<summary>git_diff_title</summary>" in notes


@pytest.mark.asyncio
async def test_entity_release_notes_git_diff_missing_remote(coordinator):
    """Test git diff generation triggers fetch when remote content is missing."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }
    coordinator.data[path] = info

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff.return_value = GitDiffResult(
        diff_text="-  name: Old\n+  name: New", is_semantic_sync=False
    )
    with patch("builtins.open", mock_open(read_data="local")):
        notes = await entity.async_generate_release_notes()

    coordinator.async_get_git_diff.assert_called_once_with("/config/blueprints/test.yaml")
    assert notes is not None
    assert "-  name: Old" in notes
    assert "+  name: New" in notes


@pytest.mark.asyncio
async def test_entity_release_notes_git_diff_source_url_normalization(coordinator):
    """Test git diff does not report source_url changes if only difference is metadata."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }

    remote_content = "blueprint:\n  name: Test\n"
    local_content = "blueprint:\n  source_url: https://url.com\n  name: Test\n"

    coordinator.data[path] = info
    info["remote_content"] = remote_content

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff.return_value = GitDiffResult(
        diff_text="", is_semantic_sync=False
    )
    with patch("builtins.open", mock_open(read_data=local_content)):
        notes = await entity.async_generate_release_notes()

    assert notes is not None
    assert "<summary>git_diff_title</summary>" not in notes
    assert "<details>" not in notes


@pytest.mark.asyncio
async def test_entity_release_notes_git_diff_cached(coordinator):
    """Test git diff returns cached notes directly."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }
    coordinator.data[path] = info

    cached_diff = "cached diff payload"
    coordinator.async_get_git_diff.return_value = GitDiffResult(
        diff_text=cached_diff, is_semantic_sync=False
    )

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    with patch("builtins.open", mock_open(read_data="local")):
        notes = await entity.async_generate_release_notes()

    assert notes is not None
    assert f"```diff\n{cached_diff}\n```" in notes
    coordinator.async_get_git_diff.assert_called_once_with("/config/blueprints/test.yaml")


@pytest.mark.asyncio
async def test_async_install_bypass_protection(coordinator):
    """Test that async_install does not use unvalidated content from diff fetch."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }
    coordinator.data[path] = info

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff = BlueprintUpdateCoordinator.async_get_git_diff.__get__(
        coordinator, BlueprintUpdateCoordinator
    )
    coordinator.async_fetch_diff_content = (
        BlueprintUpdateCoordinator.async_fetch_diff_content.__get__(
            coordinator, BlueprintUpdateCoordinator
        )
    )
    coordinator._async_fetch_with_cdn_fallback = (
        BlueprintUpdateCoordinator._async_fetch_with_cdn_fallback.__get__(
            coordinator, BlueprintUpdateCoordinator
        )
    )
    coordinator._get_cdn_url = BlueprintUpdateCoordinator._get_cdn_url
    coordinator._ensure_source_url = BlueprintUpdateCoordinator._ensure_source_url
    coordinator._normalize_url = BlueprintUpdateCoordinator._normalize_url

    with (
        patch.object(coordinator, "_is_safe_url", AsyncMock(return_value=True)),
        patch.object(
            coordinator,
            "_async_fetch_content",
            AsyncMock(return_value=("not_a_blueprint: true", "etag")),
        ),
        patch.object(
            coordinator, "_validate_blueprint", MagicMock(return_value="invalid_blueprint")
        ),
        patch("builtins.open", mock_open(read_data="local")),
    ):
        await entity.async_generate_release_notes()

    assert info.get("remote_content") is None
    with pytest.raises(HomeAssistantError):
        await entity.async_install(version=None, backup=False)
    coordinator.async_fetch_blueprint.assert_not_called()


@pytest.mark.asyncio
async def test_async_install_unsafe_url_protection(coordinator):
    """Test that async_install does not use content if URL safety check fails."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://unsafe.com",
    }
    coordinator.data[path] = info

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff = BlueprintUpdateCoordinator.async_get_git_diff.__get__(
        coordinator, BlueprintUpdateCoordinator
    )
    coordinator.async_fetch_diff_content = (
        BlueprintUpdateCoordinator.async_fetch_diff_content.__get__(
            coordinator, BlueprintUpdateCoordinator
        )
    )
    coordinator._async_fetch_with_cdn_fallback = (
        BlueprintUpdateCoordinator._async_fetch_with_cdn_fallback.__get__(
            coordinator, BlueprintUpdateCoordinator
        )
    )
    coordinator._get_cdn_url = BlueprintUpdateCoordinator._get_cdn_url
    coordinator._normalize_url = BlueprintUpdateCoordinator._normalize_url

    with (
        patch.object(coordinator, "_is_safe_url", AsyncMock(return_value=False)),
        patch("builtins.open", mock_open(read_data="local")),
    ):
        await entity.async_generate_release_notes()

    assert info.get("remote_content") is None
    assert info.get("last_error") == "unsafe_url"

    with pytest.raises(HomeAssistantError):
        await entity.async_install(version=None, backup=False)


@pytest.mark.asyncio
async def test_entity_release_notes_git_diff_with_backticks(coordinator):
    """Test git diff handles embedded backticks with dynamic fencing."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }
    coordinator.data[path] = info

    diff_text = "some diff\n```\nbackticks here\n```"
    coordinator.async_get_git_diff.return_value = GitDiffResult(
        diff_text=diff_text, is_semantic_sync=False
    )

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    with patch("builtins.open", mock_open(read_data="local")):
        notes = await entity.async_generate_release_notes()

    assert notes is not None
    assert "````diff\nsome diff\n```\nbackticks here\n```\n````" in notes


@pytest.mark.asyncio
async def test_entity_release_notes_semantic_sync_notice(coordinator):
    """Test that semantic sync notice is included in release notes."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "updatable": True,
        "source_url": "https://url.com",
    }
    coordinator.data[path] = info

    entity = BlueprintUpdateEntity(coordinator, path, info)
    entity.hass = coordinator.hass

    coordinator.async_get_git_diff.return_value = GitDiffResult(diff_text="", is_semantic_sync=True)
    expected_notice = "Source content matches after normalization"
    coordinator.async_translate.side_effect = lambda key, **kwargs: (
        expected_notice if key == "semantic_sync_notice" else key
    )

    with patch("builtins.open", mock_open(read_data="local")):
        notes = await entity.async_generate_release_notes()

    coordinator.async_translate.assert_any_call("semantic_sync_notice")
    assert notes is not None
    assert expected_notice in notes
    assert "```diff" not in notes
    assert "<details>" not in notes
    assert "<summary>git_diff_title</summary>" not in notes
