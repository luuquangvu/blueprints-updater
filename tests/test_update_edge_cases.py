"""Tests for increasing coverage of Update Entities."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.update import BlueprintUpdateEntity


@pytest.fixture
def mock_coordinator(hass):
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
        coord.data = {}
        coord.async_translate = cast(
            Any, AsyncMock(side_effect=lambda x, **kwargs: f"translated_{x}")
        )
        return coord


@pytest.mark.asyncio
async def test_update_entity_release_notes_risks(mock_coordinator):
    """Test release notes with all types of breaking risks."""
    path = "automation/test.yaml"
    info = {
        "name": "Test BP",
        "rel_path": path,
        "updatable": True,
        "source_url": "http://example.com",
        "local_hash": "12345678",
        "remote_hash": "87654321",
        "breaking_risks": [
            {"type": "new_mandatory", "args": {"input": "input_a"}},
            {"type": "missing_input", "args": {"entity": "entity_1", "input": "input_b"}},
            {"type": "removed_input", "args": {"input": "input_c", "count": 3}},
            {
                "type": "selector_mismatch",
                "args": {
                    "input": "input_d",
                    "old_type": "old",
                    "new_type": "new",
                    "count": 5,
                },
            },
            {"type": "compatibility_risk", "args": {"entity": "entity_2", "error": "err_msg"}},
            {"type": "validation_failed_blueprint", "args": {"error": "schema_err"}},
            {"type": "unknown_risk_format", "args": {}},
        ],
    }
    mock_coordinator.data = {path: info}

    with (
        patch(
            "custom_components.blueprints_updater.update.automations_with_blueprint",
            return_value=[],
        ),
        patch.object(
            mock_coordinator,
            "async_get_git_diff",
            return_value=AsyncMock(return_value=None),
        ),
    ):
        entity = BlueprintUpdateEntity(mock_coordinator, path, info)
        notes = await entity.async_release_notes()
        assert notes is not None

    assert "translated_risk_new_mandatory" in notes
    assert "translated_risk_missing_input" in notes
    assert "translated_risk_removed_input" in notes
    assert "translated_risk_selector_mismatch" in notes
    assert "translated_risk_compatibility" in notes
    assert "translated_risk_validation_failed_blueprint" in notes
    assert "translated_risk_unknown" in notes


@pytest.mark.asyncio
async def test_update_entity_remove_path(mock_coordinator, hass):
    """Test the entity removal path."""
    from custom_components.blueprints_updater.update import async_update_entities

    path = "automation/test.yaml"
    info = {"name": "Test", "rel_path": path}

    entity = BlueprintUpdateEntity(mock_coordinator, path, info)
    entity.hass = hass
    entity.entity_id = cast(Any, None)

    current_entities = {path: entity}
    mock_add = MagicMock()
    mock_entry = MagicMock()
    mock_entry.entry_id = "test_entry"

    with (
        patch.object(hass, "async_create_task") as mock_create_task,
        patch(
            "custom_components.blueprints_updater.update.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        mock_coordinator.data = {}
        async_update_entities(hass, mock_entry, mock_coordinator, current_entities, mock_add)
        mock_create_task.assert_called_once()
        coro = mock_create_task.call_args[0][0]
        assert coro.__name__ == "async_remove"
        coro.close()


def test_clear_cached_properties(mock_coordinator):
    """Test clearing cached properties."""
    path = "automation/test.yaml"
    info = {"name": "Test", "rel_path": path, "local_hash": "123456789", "updatable": False}
    mock_coordinator.data = {path: info}

    entity = BlueprintUpdateEntity(mock_coordinator, path, info)

    v1 = entity.installed_version

    mock_coordinator.data[path]["local_hash"] = "999999999"
    entity._clear_cached_properties()

    assert entity.installed_version != v1
    assert entity.installed_version == "99999999"


@pytest.mark.asyncio
async def test_extra_state_attributes(mock_coordinator):
    """Test reporting errors in extra state attributes."""
    path = "automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": path,
        "last_error": "err_key|detail",
        "breaking_risks": ["risk1"],
    }
    mock_coordinator.data = {path: info}
    info["update_blocking_reason"] = "auto_update_blocked_by_breaking_change"
    entity = BlueprintUpdateEntity(mock_coordinator, path, info)
    entity._localized_error = "Translated Error"
    entity._localized_blocking_reason = "Translated Block"

    attrs = entity.extra_state_attributes
    assert attrs["last_error"] == "Translated Error"
    assert attrs["breaking_risks"] == ["risk1"]
    assert attrs["update_blocking_reason"] == "Translated Block"
