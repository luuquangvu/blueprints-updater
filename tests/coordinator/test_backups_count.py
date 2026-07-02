"""Tests for blueprints update backups count feature."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.const import DOMAIN, DOMAIN_AUTOMATION
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.update import BlueprintUpdateEntity


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
    """Test that rotate_backups handles OSError during directory scanning gracefully."""
    file_path = tmp_path / "test_file.yaml"
    file_path.write_text("current")

    with (
        patch("os.scandir", side_effect=OSError("Permission denied")),
        patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn,
    ):
        BlueprintUpdateCoordinator._rotate_backups(str(file_path), max_bak=2)
        mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_save_file_temp_cleanup_on_exception(
    tmp_path, coordinator: BlueprintUpdateCoordinator
) -> None:
    """Test that temporary files are deleted if saving/rotation raises an exception."""
    file_path = tmp_path / "test_file.yaml"

    with (
        patch.object(coordinator, "_rotate_backups", side_effect=ValueError("Rotation failed")),
        pytest.raises(ValueError, match="Rotation failed"),
    ):
        await coordinator.async_install_blueprint(str(file_path), "content", backup=True)

    # Verify the temporary file is removed
    assert not (tmp_path / "test_file.yaml.tmp").exists()


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
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._rotate_backups",
        side_effect=ValueError("Rotation failed during restore"),
    ):
        success, msg, count = BlueprintUpdateCoordinator._execute_restore_file(
            str(real_path), version=1, max_backups=3
        )

    assert not success
    assert msg == "system_error"
    assert count == 0
    # Verify the temporary file is removed
    assert not (tmp_path / "test_file.yaml.tmp").exists()
