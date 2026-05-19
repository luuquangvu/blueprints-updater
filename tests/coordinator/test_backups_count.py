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


@pytest.mark.asyncio
async def test_restore_service_fails_when_version_exceeds_backups(hass: HomeAssistant, tmp_path):
    """Test that restore service call fails if requested version exceeds available backups."""
    from homeassistant.core import ServiceCall

    from custom_components.blueprints_updater.__init__ import async_setup, async_setup_entry

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

    def _setup_test_coordinator(hass, entry_id, coordinator):
        hass.data.setdefault(DOMAIN, {}).setdefault("coordinators", {})[entry_id] = coordinator

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
        patch("homeassistant.helpers.entity_registry.async_get") as mock_er,
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

        assert restore_handler is not None

        good_entity = MagicMock()
        good_entity.domain = "update"
        good_entity.config_entry_id = entry.entry_id
        good_entity.unique_id = BlueprintUpdateCoordinator.generate_unique_id(
            "test_entry_backup_check", "test.yaml"
        )
        mock_er.return_value.async_get.return_value = good_entity

        with pytest.raises(ServiceValidationError) as exc:
            await restore_handler(
                ServiceCall(
                    hass,
                    DOMAIN,
                    "restore_blueprint",
                    {"entity_id": "update.test", "version": 2},
                )
            )
        assert exc.value.translation_key == "missing_backup"
