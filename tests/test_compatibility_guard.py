"""Tests for Blueprints Updater Advanced Compatibility Guard logic."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
    StructuredRisk,
)


@pytest.fixture
async def coordinator(hass):
    """Create a real BlueprintUpdateCoordinator instance for tests."""
    entry = MagicMock()
    entry.domain = DOMAIN
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        instance = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    instance.data = {}
    return instance


async def _prepare_blueprint_entry(coordinator: BlueprintUpdateCoordinator, blueprint_path: str):
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
    await _prepare_blueprint_entry(coordinator, blueprint_path)

    with patch.object(
        coordinator, "_get_entities_using_blueprint", return_value=["automation.test"]
    ):
        new_content = "blueprint: name: New"
        risks: list[StructuredRisk] = [
            {"type": "compatibility_risk", "args": {"entity": "automation.test"}}
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
    assert entry["update_blocking_reason"] == "auto_update_blocked_by_breaking_change"


@pytest.mark.asyncio
async def test_auto_update_proceeds_when_risks_and_no_consumers(
    coordinator: BlueprintUpdateCoordinator,
):
    """Auto-update proceeds when risks exist but no entities use the blueprint."""
    blueprint_path = "automation/test_no_consumers.yaml"
    await _prepare_blueprint_entry(coordinator, blueprint_path)

    # No consumers
    with (
        patch.object(coordinator, "_get_entities_using_blueprint", return_value=[]),
        patch.object(coordinator, "async_install_blueprint", return_value=None),
    ):
        new_content = "blueprint: name: New"
        risks: list[StructuredRisk] = [{"type": "new_mandatory", "args": {"input": "test"}}]

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


@pytest.mark.asyncio
async def test_async_summarize_risks_formatting_and_translation_fallback(coordinator, monkeypatch):
    """Ensure async_summarize_risks formats bullets and falls back to risk_unknown."""
    translated_keys = []

    async def fake_async_translate(key, **kwargs):
        translated_keys.append(key)
        return f"translated:{key}"

    monkeypatch.setattr(coordinator, "async_translate", fake_async_translate)

    risks: list[StructuredRisk] = [
        {"type": "new_mandatory", "args": {"input": "input1"}},
        {"type": "missing_input", "args": {"input": "input2", "entity": "sensor.test"}},
        {"type": "completely_unknown", "args": {"input": "input3"}},
    ]

    summary = await coordinator.async_summarize_risks(risks)

    lines = summary.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("- ") for line in lines)

    # Verify key patterns (assuming they are prefixed with compatibility_guard. in strings.json)
    assert any("risk_new_mandatory" in key for key in translated_keys)
    assert any("risk_missing_input" in key for key in translated_keys)
    assert any("risk_unknown" in key for key in translated_keys)
