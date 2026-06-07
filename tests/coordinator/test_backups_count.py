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
