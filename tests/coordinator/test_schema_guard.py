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

    configs = {"automation.test_sensor": {"motion_sensor": "binary_sensor.motion"}}

    risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    assert any(
        risk["type"] == BlueprintRiskType.SELECTOR_MISMATCH
        and risk["args"]["input"] == "motion_sensor"
        for risk in risks
    )


def test_detect_breaking_changes_same_selector_constraint(coordinator):
    """A same-type selector domain change is retained as a compatibility risk."""
    old_content = """
blueprint:
  name: Old
  input:
    controlled_entity:
      selector:
        entity:
          domain: light
"""
    new_content = """
blueprint:
  name: New
  input:
    controlled_entity:
      selector:
        entity:
          domain: switch
"""

    risks = coordinator._detect_breaking_changes(
        old_content,
        new_content,
        {"automation.consumer": {"controlled_entity": "light.kitchen"}},
    )

    assert any(
        risk["type"] == BlueprintRiskType.SELECTOR_MISMATCH
        and risk["args"]["old_type"] == "entity"
        and risk["args"]["new_type"] == "entity"
        for risk in risks
    )


def test_normalize_selector_config_preserves_nested_presentation_keys():
    """Only presentation keys on the selector config itself are ignored."""
    normalized = BlueprintUpdateCoordinator._normalize_selector_config(
        {
            "name": "Top-level name",
            "description": "Top-level description",
            "options": [
                {
                    "value": "one",
                    "label": "Nested label",
                    "description": "Nested description",
                    "metadata": {"help": "Nested help"},
                }
            ],
        }
    )

    assert normalized == {
        "options": [
            {
                "description": "Nested description",
                "label": "Nested label",
                "metadata": {"help": "Nested help"},
                "value": "one",
            }
        ]
    }


def test_detect_breaking_changes_nested_select_label(coordinator):
    """A changed select-option label remains part of the selector contract."""
    old_content = """
blueprint:
  name: Old
  input:
    mode:
      selector:
        select:
          options:
            - value: one
              label: Original
"""
    new_content = old_content.replace("label: Original", "label: Replacement")

    risks = coordinator._detect_breaking_changes(
        old_content,
        new_content,
        {"automation.consumer": {"mode": "one"}},
    )

    assert any(risk["type"] == BlueprintRiskType.SELECTOR_MISMATCH for risk in risks)


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
      selector:
        text: {}
"""
    new_content = "blueprint:\n  name: New\n  input: {}"

    configs = {"automation.test": {"old_input": "value"}}

    risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    assert any(
        risk["type"] == BlueprintRiskType.REMOVED_INPUT and risk["args"]["input"] == "old_input"
        for risk in risks
    )


def test_detect_breaking_changes_missing_input(coordinator):
    """Test detecting missing input values for newly mandatory inputs on existing entities.

    The entity config intentionally omits the 'motion_sensor' input to trigger the
    missing_input detection when the default value is removed from the blueprint.
    """
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
    configs = {
        "automation.test": {},
        "automation.no_input": {},
    }

    risks = coordinator._detect_breaking_changes(old_content, new_content, configs)

    found_entities = {
        risk["args"]["entity"]
        for risk in risks
        if risk["type"] == BlueprintRiskType.MISSING_INPUT
        and risk["args"]["input"] == "motion_sensor"
    }
    assert found_entities == {"automation.test", "automation.no_input"}
