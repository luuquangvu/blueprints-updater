"""Tests for specific error handling and edge cases in BlueprintUpdateCoordinator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Mock coordinator."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__", return_value=None
    ):
        from datetime import timedelta

        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord.config_entry = entry
        coord.setup_complete = True
        return coord


@pytest.mark.asyncio
async def test_async_translate_error_handling(coordinator):
    """Test error handling in async_translate."""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        side_effect=OSError("Disk full"),
    ):
        result = await coordinator.async_translate("test_key")
        assert result == "test_key"


@pytest.mark.asyncio
async def test_extract_inputs_schema_malformed(coordinator):
    """Test _extract_inputs_schema with malformed YAML."""
    schema, error = coordinator._extract_inputs_schema("not a yaml dict")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("automation: test")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("blueprint: { input: [] }")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("blueprint: { input: { test: true } }")
    assert schema["test"]["mandatory"] is True
    assert error is None


def test_extract_inputs_schema_exception(coordinator):
    """Test _extract_inputs_schema with forced exception."""
    from homeassistant.exceptions import HomeAssistantError

    with patch("homeassistant.util.yaml.parse_yaml", side_effect=HomeAssistantError("forced")):
        schema, error = coordinator._extract_inputs_schema("any")
        assert schema == {}
        assert error == "forced"


@pytest.mark.asyncio
async def test_merge_previous_data_edge_cases(coordinator):
    """Test merge_previous_data with malformed previous data."""
    results = {"path1": {"local_hash": "A", "remote_hash": "B", "updatable": True}}

    coordinator.data = {}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is True
    results = {"path1": {"local_hash": "A", "remote_hash": "A", "updatable": False}}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is False

    coordinator.data = {"path1": "not a dict"}
    results = {"path1": {"local_hash": "A", "remote_hash": "B", "updatable": True}}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is True


@pytest.mark.asyncio
async def test_prune_stale_metadata(coordinator):
    """Test pruning logic for stale metadata."""
    coordinator._persisted_etags = {"path1": "etag1", "path2": "etag2"}
    coordinator._persisted_hashes = {"path1": "hash1", "path2": "hash2"}
    coordinator.data = {"path1": {}}

    with (
        patch("os.path.isfile", side_effect=lambda x: x == "path1"),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock) as mock_save,
        patch.object(
            coordinator.hass,
            "async_create_background_task",
            side_effect=lambda coro, name=None: coro,
        ) as mock_bg,
    ):
        await coordinator._async_prune_stale_metadata({"path1"})
        for call in mock_bg.call_args_list:
            if call.kwargs.get("name") == f"{DOMAIN}_prune_save":
                await call.args[0]
        mock_save.assert_awaited_once_with(force=True)

    assert "path2" not in coordinator._persisted_etags
    assert "path2" not in coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_save_metadata_safety(coordinator):
    """Test safety check in save_metadata."""
    coordinator.setup_complete = False
    with patch.object(coordinator._store, "async_save") as mock_save:
        await coordinator._async_save_metadata()
        mock_save.assert_not_called()
