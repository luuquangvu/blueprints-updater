"""Tests for blueprints update backups count feature."""

import asyncio
import gc
import os
from unittest.mock import AsyncMock, MagicMock, patch
from weakref import ref

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.const import DOMAIN, DOMAIN_AUTOMATION
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.file_store import BlueprintFileStore
from custom_components.blueprints_updater.update import BlueprintUpdateEntity


@pytest.mark.asyncio
async def test_same_path_installs_are_serialized(coordinator):
    """Two install entry points cannot mutate one canonical path concurrently."""
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    maximum_active = 0
    calls = 0

    async def fake_locked_install(*_args, **_kwargs):
        """Pause the first locked transaction while a second task starts."""
        nonlocal active, calls, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if calls == 1:
            first_entered.set()
            await release_first.wait()
        active -= 1

    coordinator._async_install_blueprint_locked = fake_locked_install
    path = "/config/blueprints/automation/serialized.yaml"
    first = asyncio.create_task(coordinator.async_install_blueprint(path, "first"))
    await first_entered.wait()
    second = asyncio.create_task(coordinator.async_install_blueprint(path, "second"))
    yielded = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(yielded.set_result, None)
    await yielded

    assert calls == 1
    release_first.set()
    await asyncio.gather(first, second)
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_unused_path_lock_is_released(tmp_path) -> None:
    """The path-lock registry does not retain an idle lock indefinitely."""
    path = str(tmp_path / "transient.yaml")
    canonical_path = os.path.realpath(path)

    async with BlueprintFileStore.transaction(path):
        lock_reference = ref(BlueprintFileStore._path_locks[canonical_path])
        assert lock_reference() is not None

    gc.collect()
    assert lock_reference() is None
    assert canonical_path not in BlueprintFileStore._path_locks


def test_temp_write_failure_closes_stream_without_raw_descriptor_close(tmp_path) -> None:
    """A stream-owned descriptor is not closed again through its raw integer."""
    target = tmp_path / "test.yaml"
    real_close = os.close

    with (
        patch(
            "custom_components.blueprints_updater.file_store.os.fsync",
            side_effect=OSError("sync failed"),
        ),
        patch(
            "custom_components.blueprints_updater.file_store.os.close",
            wraps=real_close,
        ) as close_descriptor,
        pytest.raises(OSError, match="sync failed"),
    ):
        BlueprintFileStore._write_temp(str(target), "content")

    close_descriptor.assert_not_called()
    assert not list(tmp_path.glob("*.tmp"))


def test_temp_write_fdopen_failure_closes_raw_descriptor(tmp_path) -> None:
    """Failure to transfer descriptor ownership still closes the raw descriptor."""
    target = tmp_path / "test.yaml"
    temp_path = tmp_path / ".test.yaml.failed.tmp"
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT)
    real_close = os.close

    with (
        patch.object(
            BlueprintFileStore,
            "_new_temp_path",
            return_value=(descriptor, str(temp_path)),
        ),
        patch(
            "custom_components.blueprints_updater.file_store.os.fdopen",
            side_effect=OSError("fdopen failed"),
        ),
        patch(
            "custom_components.blueprints_updater.file_store.os.close",
            wraps=real_close,
        ) as close_descriptor,
        pytest.raises(OSError, match="fdopen failed"),
    ):
        BlueprintFileStore._write_temp(str(target), "content")

    close_descriptor.assert_called_once_with(descriptor)
    assert not temp_path.exists()


def test_install_rejects_corrupt_temp_before_replacing_target(tmp_path) -> None:
    """A corrupted temporary write never replaces the active blueprint."""
    target = tmp_path / "test.yaml"
    temp_path = tmp_path / ".test.yaml.corrupt.tmp"
    target.write_text("original", encoding="utf-8")
    temp_path.write_text("corrupted", encoding="utf-8")
    expected_hash = BlueprintFileStore._hash_file(str(target))

    with (
        patch.object(
            BlueprintFileStore,
            "_write_temp",
            return_value=(str(temp_path), expected_hash),
        ),
        patch.object(BlueprintFileStore, "rotate_backups") as rotate_backups,
        pytest.raises(OSError, match="Installed blueprint verification failed"),
    ):
        BlueprintFileStore.install(
            str(target),
            "replacement",
            max_backups=3,
            create_backup=True,
        )

    assert target.read_text(encoding="utf-8") == "original"
    assert not temp_path.exists()
    rotate_backups.assert_not_called()


@pytest.mark.asyncio
async def test_requested_backup_failure_aborts_install(coordinator, tmp_path):
    """A requested backup failure leaves the active blueprint unchanged."""
    blueprint_path = tmp_path / "test.yaml"
    original = "blueprint:\n  name: Original\n  domain: automation\n"
    replacement = "blueprint:\n  name: Replacement\n  domain: automation\n"
    blueprint_path.write_text(original, encoding="utf-8")

    with (
        patch(
            "custom_components.blueprints_updater.file_store.shutil.copy2",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        await coordinator.async_install_blueprint(
            str(blueprint_path),
            replacement,
            reload_services=False,
            backup=True,
        )

    assert blueprint_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_invalid_backup_is_rejected_before_restore(coordinator, tmp_path):
    """Invalid backup YAML never replaces a valid active blueprint."""
    blueprint_path = tmp_path / "test.yaml"
    source_url = "https://example.com/test.yaml"
    original = f"blueprint:\n  name: Original\n  domain: automation\n  source_url: {source_url}\n"
    blueprint_path.write_text(original, encoding="utf-8")
    (tmp_path / "test.yaml.bak.1").write_text("invalid: yaml: [", encoding="utf-8")
    coordinator.data[str(blueprint_path)] = {
        "name": "Original",
        "domain": DOMAIN_AUTOMATION,
        "relative_path": "automation/test.yaml",
        "source_url": source_url,
    }

    result = await coordinator.async_restore_blueprint(str(blueprint_path))

    assert result["success"] is False
    assert result["translation_key"] == "invalid_yaml"
    assert blueprint_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_restore_source_mismatch_returns_validation_error(coordinator, tmp_path):
    """A backup from another source is reported as validation, not system failure."""
    blueprint_path = tmp_path / "test.yaml"
    tracked_url = "https://example.com/tracked.yaml"
    backup_url = "https://example.com/different.yaml"
    original = f"blueprint:\n  name: Original\n  domain: automation\n  source_url: {tracked_url}\n"
    backup = f"blueprint:\n  name: Backup\n  domain: automation\n  source_url: {backup_url}\n"
    blueprint_path.write_text(original, encoding="utf-8")
    (tmp_path / "test.yaml.bak.1").write_text(backup, encoding="utf-8")
    coordinator.data[str(blueprint_path)] = {
        "name": "Original",
        "domain": DOMAIN_AUTOMATION,
        "relative_path": "automation/test.yaml",
        "source_url": tracked_url,
    }

    result = await coordinator.async_restore_blueprint(str(blueprint_path))

    assert result["success"] is False
    assert result["translation_key"] == "blueprint_validation_error"
    assert result["translation_kwargs"] == {
        "error": "Backup source URL does not match the tracked blueprint"
    }
    assert blueprint_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_coordinator_count_backups_sync(coordinator, tmp_path):
    """Test standard counting of backups on disk."""
    bp_file = tmp_path / "test_count.yaml"
    bp_file.write_text("v0")

    (tmp_path / "test_count.yaml.bak.1").write_text("v1")
    (tmp_path / "test_count.yaml.bak.2").write_text("v2")

    count = coordinator._count_backups_sync(str(bp_file), max_bak=5)
    assert count == 2


@pytest.mark.asyncio
async def test_entity_extra_state_attributes_includes_backups_count(coordinator):
    """Test that the update entity extra state attributes include backups_count."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {
        path: {
            "name": "Test",
            "relative_path": "test.yaml",
            "domain": DOMAIN_AUTOMATION,
            "backups_count": 3,
            "provider_type": "generic",
            "updatable": True,
        }
    }
    entity = BlueprintUpdateEntity(coordinator, path, coordinator.data[path])
    entity.hass = coordinator.hass
    entity.entity_id = "update.test"

    attrs = entity.extra_state_attributes
    assert attrs["backups_count"] == 3


async def _setup_restore_test_context(hass, entry, coordinator_mock):
    """Set up the test registration and return the restore handler and mock register."""
    from custom_components.blueprints_updater.__init__ import async_setup, async_setup_entry

    def _setup_test_coordinator(h, entry_id, coord):
        """Set up test coordinator in hass data."""
        h.data.setdefault(DOMAIN, {}).setdefault("coordinators", {})[entry_id] = coord

    _setup_test_coordinator(hass, entry.entry_id, coordinator_mock)

    with (
        patch(
            "custom_components.blueprints_updater.__init__.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ) as mock_coordinator_class,
        patch(
            "custom_components.blueprints_updater.__init__.async_register_admin_service"
        ) as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        mock_coordinator_class.generate_unique_id = BlueprintUpdateCoordinator.generate_unique_id
        await async_setup(hass, {})
        await async_setup_entry(hass, entry)

        restore_handler = next(
            (
                call.args[3] if len(call.args) > 3 else call.kwargs.get("service_func")
                for call in mock_register.call_args_list
                if (len(call.args) > 2 and call.args[2] == "restore_blueprint")
                or call.kwargs.get("service") == "restore_blueprint"
            ),
            None,
        )
        return restore_handler, mock_register


@pytest.mark.asyncio
async def test_restore_blueprint_registration(hass: HomeAssistant):
    """Test that restore blueprint service registers correctly on setup."""
    entry = MagicMock()
    entry.entry_id = "test_entry_backup_check"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.config_entry = entry
    coordinator_mock.hass = hass
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.data = {}

    _, mock_register = await _setup_restore_test_context(hass, entry, coordinator_mock)
    assert any(
        (len(call.args) > 2 and call.args[2] == "restore_blueprint")
        or call.kwargs.get("service") == "restore_blueprint"
        for call in mock_register.call_args_list
    )


@pytest.mark.asyncio
async def test_restore_blueprint_handler_exists(hass: HomeAssistant):
    """Test that the restore blueprint handler is correctly resolved."""
    entry = MagicMock()
    entry.entry_id = "test_entry_backup_check"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.config_entry = entry
    coordinator_mock.hass = hass
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.data = {}

    handler, _ = await _setup_restore_test_context(hass, entry, coordinator_mock)
    assert handler is not None


@pytest.mark.asyncio
async def test_restore_blueprint_validation_fails_on_missing_backup(
    hass: HomeAssistant,
):
    """Test that the restore blueprint handler validation fails for a missing backup version."""
    from homeassistant.core import ServiceCall

    entry = MagicMock()
    entry.entry_id = "test_entry_backup_check"
    entry.options = {"max_backups": 3}
    entry.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.config_entry = entry
    coordinator_mock.data = {
        "test.yaml": {
            "relative_path": "test.yaml",
            "updatable": True,
            "backups_count": 1,
        }
    }
    coordinator_mock.hass = hass
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock._count_backups_sync = BlueprintUpdateCoordinator._count_backups_sync
    coordinator_mock.async_check_backup_exists = AsyncMock(return_value=False)
    coordinator_mock.async_restore_blueprint = AsyncMock(
        return_value={"success": False, "translation_key": "missing_backup"}
    )

    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er:
        handler, _ = await _setup_restore_test_context(hass, entry, coordinator_mock)
        assert handler is not None

        good_entity = MagicMock()
        good_entity.domain = "update"
        good_entity.config_entry_id = entry.entry_id
        good_entity.unique_id = BlueprintUpdateCoordinator.generate_unique_id(
            "test_entry_backup_check", "test.yaml"
        )
        mock_er.return_value.async_get.return_value = good_entity

        with pytest.raises(ServiceValidationError) as exc:
            await handler(
                ServiceCall(
                    hass,
                    DOMAIN,
                    "restore_blueprint",
                    {"entity_id": "update.test", "version": 2},
                )
            )
        assert exc.value.translation_key == "missing_backup"


@pytest.mark.asyncio
async def test_coordinator_count_backups_no_backups(coordinator, tmp_path):
    """Test backups count when there are no backup files."""
    bp_file = tmp_path / "test_none.yaml"
    bp_file.write_text("v0")

    count = coordinator._count_backups_sync(str(bp_file), max_bak=5)
    assert count == 0


@pytest.mark.asyncio
async def test_coordinator_count_backups_non_contiguous(coordinator, tmp_path):
    """Test backups count when indices are non-contiguous."""
    bp_file = tmp_path / "test_non_contiguous.yaml"
    bp_file.write_text("v0")

    (tmp_path / "test_non_contiguous.yaml.bak.1").write_text("v1")
    (tmp_path / "test_non_contiguous.yaml.bak.3").write_text("v3")

    count = coordinator._count_backups_sync(str(bp_file), max_bak=5)
    assert count == 2


@pytest.mark.asyncio
async def test_coordinator_count_backups_respects_max_bak(coordinator, tmp_path):
    """Test backups count only counts up to max_bak."""
    bp_file = tmp_path / "test_max.yaml"
    bp_file.write_text("v0")

    for i in range(1, 6):
        (tmp_path / f"test_max.yaml.bak.{i}").write_text(f"v{i}")

    count = coordinator._count_backups_sync(str(bp_file), max_bak=3)
    assert count == 3


@pytest.mark.asyncio
async def test_coordinator_check_backup_exists(coordinator, tmp_path):
    """Test checking if backups exist."""
    bp_file = tmp_path / "test_exists.yaml"
    bp_file.write_text("v0")

    assert not coordinator._check_backup_exists_sync(str(bp_file), 1)
    assert not await coordinator.async_check_backup_exists(str(bp_file), 1)

    (tmp_path / "test_exists.yaml.bak.1").write_text("v1")
    assert coordinator._check_backup_exists_sync(str(bp_file), 1)
    assert await coordinator.async_check_backup_exists(str(bp_file), 1)

    (tmp_path / "test_exists.yaml.bak.2").write_text("v2")
    assert coordinator._check_backup_exists_sync(str(bp_file), 2)
    assert await coordinator.async_check_backup_exists(str(bp_file), 2)


@pytest.mark.asyncio
async def test_rotate_backups_limit_reduction(tmp_path) -> None:
    """Test that rotate_backups cleans up leftover backups when limits are reduced."""
    file_path = tmp_path / "test_file.yaml"
    file_path.write_text("current")
    for i in range(1, 6):
        (tmp_path / f"test_file.yaml.bak.{i}").write_text(f"bak{i}")

    BlueprintUpdateCoordinator._rotate_backups(str(file_path), max_bak=2)

    # Check bak.1 and bak.2 exist
    assert (tmp_path / "test_file.yaml.bak.1").read_text() == "current"
    assert (tmp_path / "test_file.yaml.bak.2").read_text() == "bak1"
    # Leftover bak.3, bak.4, bak.5 should be cleaned up
    assert not (tmp_path / "test_file.yaml.bak.3").exists()
    assert not (tmp_path / "test_file.yaml.bak.4").exists()
    assert not (tmp_path / "test_file.yaml.bak.5").exists()


@pytest.mark.asyncio
async def test_rotate_backups_malformed_suffixes(tmp_path) -> None:
    """Test that rotate_backups ignores malformed backup suffixes."""
    file_path = tmp_path / "test_file.yaml"
    file_path.write_text("current")
    (tmp_path / "test_file.yaml.bak.abc").write_text("malformed1")
    (tmp_path / "test_file.yaml.bak.1.tmp").write_text("malformed2")
    (tmp_path / "test_file.yaml.bak.1").write_text("valid1")

    # Run backup rotation with limit reduction to 0
    BlueprintUpdateCoordinator._rotate_backups(str(file_path), max_bak=0)

    # Valid backup should be deleted
    assert not (tmp_path / "test_file.yaml.bak.1").exists()
    # Malformed ones should be ignored and still exist
    assert (tmp_path / "test_file.yaml.bak.abc").exists()
    assert (tmp_path / "test_file.yaml.bak.1.tmp").exists()


@pytest.mark.asyncio
async def test_rotate_backups_scandir_oserror(tmp_path) -> None:
    """Test that backup scanning errors fail closed."""
    file_path = tmp_path / "test_file.yaml"
    file_path.write_text("current")

    with (
        patch("os.scandir", side_effect=OSError("Permission denied")),
        pytest.raises(OSError, match="Permission denied"),
    ):
        BlueprintUpdateCoordinator._rotate_backups(str(file_path), max_bak=2)


def test_stale_backup_entry_error_does_not_abort_discovery(tmp_path, caplog) -> None:
    """One unreadable directory entry does not hide other stale backups."""
    file_path = tmp_path / "test_file.yaml"
    newest_backup = tmp_path / "test_file.yaml.bak.1"
    unreadable_backup = tmp_path / "test_file.yaml.bak.3"
    removable_backup = tmp_path / "test_file.yaml.bak.4"
    file_path.write_text("current")
    newest_backup.write_text("previous")
    unreadable_backup.write_text("unreadable")
    removable_backup.write_text("stale")

    unreadable_entry = MagicMock(
        name="unreadable_entry",
        path=str(unreadable_backup),
    )
    unreadable_entry.name = unreadable_backup.name
    unreadable_entry.is_file.side_effect = PermissionError("entry metadata unavailable")
    removable_entry = MagicMock(
        name="removable_entry",
        path=str(removable_backup),
    )
    removable_entry.name = removable_backup.name
    removable_entry.is_file.return_value = True
    entries = MagicMock()
    entries.__enter__.return_value = iter((unreadable_entry, removable_entry))

    with patch(
        "custom_components.blueprints_updater.file_store.os.scandir",
        return_value=entries,
    ):
        BlueprintFileStore.rotate_backups(str(file_path), max_backups=2)

    assert newest_backup.read_text() == "current"
    assert (tmp_path / "test_file.yaml.bak.2").read_text() == "previous"
    assert unreadable_backup.read_text() == "unreadable"
    assert not removable_backup.exists()
    assert "Failed to inspect stale backup" in caplog.text


def test_stale_backup_removal_error_does_not_block_verified_backup(tmp_path, caplog) -> None:
    """Stale retention cleanup is best effort and does not block a new backup."""
    file_path = tmp_path / "test_file.yaml"
    newest_backup = tmp_path / "test_file.yaml.bak.1"
    stale_backup = tmp_path / "test_file.yaml.bak.3"
    file_path.write_text("current")
    newest_backup.write_text("previous")
    stale_backup.write_text("stale")
    real_remove = os.remove

    def remove_unless_stale(path: str) -> None:
        """Simulate one undeletable out-of-range backup."""
        if os.fspath(path) == str(stale_backup):
            raise PermissionError("read-only stale backup")
        real_remove(path)

    with patch(
        "custom_components.blueprints_updater.file_store.os.remove",
        side_effect=remove_unless_stale,
    ):
        BlueprintFileStore.rotate_backups(str(file_path), max_backups=2)

    assert newest_backup.read_text() == "current"
    assert (tmp_path / "test_file.yaml.bak.2").read_text() == "previous"
    assert stale_backup.read_text() == "stale"
    assert "Failed to remove stale backup" in caplog.text


@pytest.mark.asyncio
async def test_save_file_temp_cleanup_on_exception(
    tmp_path, coordinator: BlueprintUpdateCoordinator
) -> None:
    """Test that temporary files are deleted if saving/rotation raises an exception."""
    file_path = tmp_path / "test_file.yaml"

    with (
        patch(
            "custom_components.blueprints_updater.file_store.BlueprintFileStore.rotate_backups",
            side_effect=ValueError("Rotation failed"),
        ),
        pytest.raises(ValueError, match="Rotation failed"),
    ):
        await coordinator.async_install_blueprint(str(file_path), "content", backup=True)

    # Verify the temporary file is removed
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_save_file_happy_path(tmp_path, coordinator: BlueprintUpdateCoordinator) -> None:
    """Test the happy path of saving a file successfully with backup rotation."""
    file_path = tmp_path / "test_file.yaml"
    file_path.write_text("old_content")

    # Perform successful blueprint install
    await coordinator.async_install_blueprint(str(file_path), "new_content", backup=True)

    assert file_path.read_text() == "new_content"
    assert (tmp_path / "test_file.yaml.bak.1").read_text() == "old_content"


@pytest.mark.asyncio
async def test_execute_restore_file_happy_path(tmp_path) -> None:
    """Test the happy path of _execute_restore_file."""
    real_path = tmp_path / "test_file.yaml"
    real_path.write_text("current")
    bak_path = tmp_path / "test_file.yaml.bak.1"
    bak_path.write_text("backup_content")

    success, msg, _count = BlueprintUpdateCoordinator._execute_restore_file(
        str(real_path), version=1, max_backups=3
    )

    assert success
    assert msg == "success"
    assert real_path.read_text() == "backup_content"


@pytest.mark.asyncio
async def test_execute_restore_file_temp_cleanup_on_exception(tmp_path) -> None:
    """Test that restore_file temporary files are cleaned up on exception."""
    real_path = tmp_path / "test_file.yaml"
    bak_path = tmp_path / "test_file.yaml.bak.1"
    bak_path.write_text("backup_content")

    # Patch _rotate_backups to fail during restoration
    with patch(
        "custom_components.blueprints_updater.file_store.BlueprintFileStore.rotate_backups",
        side_effect=ValueError("Rotation failed during restore"),
    ):
        success, msg, count = BlueprintUpdateCoordinator._execute_restore_file(
            str(real_path), version=1, max_backups=3
        )

    assert not success
    assert msg == "system_error"
    assert count == 0
    # Verify the temporary file is removed
    assert not list(tmp_path.glob(".*.tmp"))
