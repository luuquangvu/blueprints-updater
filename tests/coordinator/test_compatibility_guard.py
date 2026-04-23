"""Tests for Blueprints Updater Advanced Compatibility Guard logic."""

from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import (
    BlueprintBlockingReason,
    BlueprintRiskType,
    BlueprintUpdateCoordinator,
    StructuredRisk,
)


@pytest.fixture
def coordinator(hass):
    """Create a real BlueprintUpdateCoordinator instance for tests."""
    entry = MagicMock()
    entry.domain = DOMAIN
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        instance = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    instance.hass = hass
    instance.config_entry = entry
    instance.setup_complete = True
    instance.data = {}

    instance.async_translate = cast(
        Any, AsyncMock(side_effect=lambda key, **kwargs: f"translated:{key}")
    )

    return instance


def _prepare_blueprint_entry(coordinator: BlueprintUpdateCoordinator, blueprint_path: str):
    """Helper to pre-populate coordinator state for a blueprint."""
    coordinator.data[blueprint_path] = {
        "updatable": True,
        "breaking_risks": [],
        "update_blocking_reason": None,
        "name": "Test Blueprint",
        "rel_path": blueprint_path,
    }


@pytest.mark.asyncio
async def test_auto_update_guard_blocks_when_risks_present(coordinator: BlueprintUpdateCoordinator):
    """Auto-update is blocked when compatibility risk is present and entities are in use."""
    blueprint_path = "automation/test_blueprint.yaml"
    _prepare_blueprint_entry(coordinator, blueprint_path)

    with (
        patch.object(
            coordinator, "_get_entities_using_blueprint", return_value=["automation.test"]
        ),
        patch.object(coordinator, "_async_send_auto_update_notification", return_value=None),
    ):
        new_content = "blueprint: name: New"
        risks: list[StructuredRisk] = [
            {
                "type": BlueprintRiskType.COMPATIBILITY,
                "args": {"entity": "automation.test", "error": "Incompatible change"},
            }
        ]

        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            new_content,
            "new_hash",
            "new_etag",
            risks,
            [],
            set(),
        )

    assert result is True
    entry = coordinator.data[blueprint_path]
    assert entry["updatable"] is True
    assert entry["update_blocking_reason"] == BlueprintBlockingReason.BREAKING_CHANGE


@pytest.mark.asyncio
async def test_auto_update_proceeds_when_risks_and_no_consumers(
    coordinator: BlueprintUpdateCoordinator,
):
    """Auto-update proceeds when risks exist but no entities use the blueprint."""
    blueprint_path = "automation/test_no_consumers.yaml"
    _prepare_blueprint_entry(coordinator, blueprint_path)

    async def mock_install(path, *args, **kwargs):
        if coordinator.data and path in coordinator.data:
            coordinator.data[path].update({"updatable": False})

    with (
        patch.object(coordinator, "_get_entities_using_blueprint", return_value=[]),
        patch.object(coordinator, "async_install_blueprint", side_effect=mock_install),
    ):
        new_content = "blueprint: name: New"
        risks: list[StructuredRisk] = [
            {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "test"}}
        ]

        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            new_content,
            "new_hash",
            "new_etag",
            risks,
            [],
            set(),
        )

    assert result is True
    entry = coordinator.data[blueprint_path]
    assert entry["updatable"] is False
    assert entry["update_blocking_reason"] is None
    assert entry["breaking_risks"] == []


@pytest.mark.asyncio
async def test_async_summarize_risks_formatting_and_translation_fallback(coordinator, monkeypatch):
    """Ensure async_summarize_risks formats bullets and falls back to risk_unknown."""
    translated_keys = []

    async def fake_async_translate(key, **kwargs):
        translated_keys.append(key)
        return f"translated:{key}"

    monkeypatch.setattr(coordinator, "async_translate", fake_async_translate)

    risks: list[StructuredRisk] = cast(
        list[StructuredRisk],
        [
            {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "input1"}},
            {
                "type": BlueprintRiskType.MISSING_INPUT,
                "args": {"input": "input2", "entity": "sensor.test"},
            },
            {"type": "completely_unknown", "args": {"input": "input3"}},
        ],
    )

    summary = await coordinator.async_summarize_risks(risks)

    lines = summary.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("- ") for line in lines)

    assert any("risk_new_mandatory" in key for key in translated_keys)
    assert any("risk_missing_input" in key for key in translated_keys)
    assert any("risk_unknown" in key for key in translated_keys)


@pytest.mark.asyncio
async def test_get_risk_summary_shim(coordinator: BlueprintUpdateCoordinator, monkeypatch):
    """Verify that the _get_risk_summary legacy shim correctly calls async_summarize_risks."""
    translated_keys = []

    async def fake_async_translate(key, **kwargs):
        translated_keys.append(key)
        return f"translated:{key}"

    monkeypatch.setattr(coordinator, "async_translate", fake_async_translate)

    risks: list[StructuredRisk] = cast(
        list[StructuredRisk],
        [
            {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "input1"}},
            {"type": "unknown_risk_type", "args": {"foo": "bar"}},
        ],
    )

    summary = await coordinator._get_risk_summary(risks)

    assert "- translated:risk_new_mandatory" in summary
    assert "- translated:risk_unknown" in summary
    assert "\n" in summary
    assert any("risk_new_mandatory" in k for k in translated_keys)
    assert any("risk_unknown" in k for k in translated_keys)


@pytest.mark.asyncio
async def test_auto_update_guard_blocks_on_system_error(coordinator: BlueprintUpdateCoordinator):
    """Auto-update is blocked when a system error risk is present, even without consumers."""
    blueprint_path = "automation/system_error.yaml"
    _prepare_blueprint_entry(coordinator, blueprint_path)

    with (
        patch.object(coordinator, "_get_entities_using_blueprint", return_value=[]),
        patch.object(coordinator, "_async_send_auto_update_notification", return_value=None),
        patch.object(coordinator, "async_translate", side_effect=lambda key, **kwargs: key),
    ):
        risks: list[StructuredRisk] = [
            {"type": BlueprintRiskType.SYSTEM_ERROR, "args": {"error": "Critical fail"}}
        ]

        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            "blueprint: name: New",
            "new_hash",
            "new_etag",
            risks,
            [],
            set(),
        )

    assert result is True
    entry = coordinator.data[blueprint_path]
    assert entry["updatable"] is True
    assert entry["update_blocking_reason"] == BlueprintBlockingReason.SYSTEM_ERROR
