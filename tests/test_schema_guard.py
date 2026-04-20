"""Tests for Blueprints Updater Schema Guard logic."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import (
    BlueprintRiskType,
    BlueprintUpdateCoordinator,
)


@pytest.fixture(autouse=True)
def mock_frame_helper():
    """Mock HA frame helper to avoid setup errors."""
    with patch("homeassistant.helpers.frame.report_usage"):
        yield


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.options = {"auto_update": True}
    return BlueprintUpdateCoordinator(hass, config_entry, update_interval=timedelta(hours=1))


def test_detect_breaking_changes_selector_mismatch(coordinator):
    """Test detecting selector mismatch as a breaking change."""
    old_content = """
blueprint:
  name: Old
  input:
    motion_sensor:
      name: Sensor
      selector:
        entity:
          domain: binary_sensor
"""
    new_content = """
blueprint:
  name: New
  input:
    motion_sensor:
      name: Sensor
      selector:
        boolean: {}
"""

    entities = ["automation.test_sensor"]
    configs = {
        "automation.test_sensor": {
            "use_blueprint": {
                "path": "automation/test.yaml",
                "input": {"motion_sensor": "binary_sensor.motion"},
            }
        }
    }

    with (
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=entities),
        patch.object(coordinator, "_get_entities_configs", return_value=configs),
    ):
        risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    assert any(
        risk["type"] == BlueprintRiskType.SELECTOR_MISMATCH
        and risk["args"]["input"] == "motion_sensor"
        for risk in risks
    )


def test_detect_breaking_changes_new_mandatory(coordinator):
    """Test detecting new mandatory input."""
    old_content = "blueprint:\n  name: Old\n  input: {}"
    new_content = """
blueprint:
  name: New
  input:
    new_input:
      name: New
      selector:
        text: {}
"""

    risks = coordinator._detect_breaking_changes(old_content, new_content, {})
    assert any(
        risk["type"] == BlueprintRiskType.NEW_MANDATORY and risk["args"]["input"] == "new_input"
        for risk in risks
    )


def test_detect_breaking_changes_removed_input(coordinator):
    """Test detecting removed input that is in use."""
    old_content = """
blueprint:
  name: Old
  input:
    old_input:
      name: Old
"""
    new_content = "blueprint:\n  name: New\n  input: {}"

    entities = ["automation.test"]
    configs = {
        "automation.test": {
            "use_blueprint": {"path": "automation/test.yaml", "input": {"old_input": "value"}}
        }
    }

    with (
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=entities),
        patch.object(coordinator, "_get_entities_configs", return_value=configs),
    ):
        risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    assert any(
        risk["type"] == BlueprintRiskType.REMOVED_INPUT and risk["args"]["input"] == "old_input"
        for risk in risks
    )


def test_detect_breaking_changes_missing_input(coordinator):
    """Test detecting missing input values for newly mandatory inputs on existing entities."""
    old_content = """
blueprint:
  name: Old
  domain: automation
  input:
    motion_sensor:
      name: Sensor
      selector:
        entity:
          domain: binary_sensor
      default: binary_sensor.motion
"""
    new_content = """
blueprint:
  name: New
  domain: automation
  input:
    motion_sensor:
      name: Sensor
      selector:
        entity:
          domain: binary_sensor
"""
    rel_path = "automation/test.yaml"
    entities = ["automation.test"]
    configs = {
        "automation.test": {
            "use_blueprint": {
                "path": rel_path,
                "input": {
                    # Intentionally omit "motion_sensor" to trigger missing_input
                },
            },
        }
    }

    with (
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=entities),
        patch.object(coordinator, "_get_entities_configs", return_value=configs),
    ):
        risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    assert any(
        risk["type"] == BlueprintRiskType.MISSING_INPUT
        and risk["args"]["entity"] == "automation.test"
        and risk["args"]["input"] == "motion_sensor"
        for risk in risks
    )
