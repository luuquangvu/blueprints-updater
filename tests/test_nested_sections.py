"""Tests for nested blueprint sections in Blueprints Updater."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import (
    BlueprintRiskType,
    BlueprintUpdateCoordinator,
)


@pytest.mark.asyncio
async def test_extract_inputs_schema_nested_sections():
    """Test that _extract_inputs_schema correctly flattens nested sections."""
    content = """
blueprint:
  name: Nested Test
  domain: automation
  input:
    section_one:
      name: Section 1
      input:
        input_one:
          name: Input 1
          selector:
            entity:
        input_two:
          name: Input 2
          default: something
    input_three:
      name: Input 3
      selector:
        boolean: {}
    section_two:
      name: Section 2
      input:
        nested_section:
          name: Inner Section
          input:
            input_four:
              name: Input 4
    """

    schema, error = BlueprintUpdateCoordinator._extract_inputs_schema(content)

    assert error is None
    assert "section_one" not in schema
    assert "section_two" not in schema
    assert "nested_section" not in schema

    assert "input_one" in schema
    assert schema["input_one"]["mandatory"] is True
    assert schema["input_one"]["selector"] == "entity"

    assert "input_two" in schema
    assert schema["input_two"]["mandatory"] is False
    assert schema["input_two"]["selector"] is None

    assert "input_three" in schema
    assert schema["input_three"]["mandatory"] is True
    assert schema["input_three"]["selector"] == "boolean"

    assert "input_four" in schema
    assert schema["input_four"]["mandatory"] is True


@pytest.mark.asyncio
async def test_breaking_change_detection_with_sections(hass):
    """Test that breaking changes are detected inside sections."""
    old_content = """
blueprint:
  name: Test
  input:
    section:
      input:
        target_input:
          default: old
    """
    new_content = """
blueprint:
  name: Test
  input:
    section:
      input:
        target_input:
          # removed default -> mandatory
    """

    entry = MagicMock()
    entry.domain = DOMAIN
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    with (
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=[]),
        patch.object(coordinator, "_get_entities_configs", return_value={}),
    ):
        risks = coordinator._detect_breaking_changes(old_content, new_content, {})

    assert any(
        r["type"] == BlueprintRiskType.NEW_MANDATORY and r["args"]["input"] == "target_input"
        for r in risks
    )


@pytest.mark.asyncio
async def test_no_false_positive_on_section_changes(hass):
    """Test that adding or removing a section header does not trigger a risk."""
    old_content = """
blueprint:
  name: Test
  input:
    input_one:
      default: val
    """
    new_content = """
blueprint:
  name: Test
  input:
    my_new_section:
      name: New UI Group
      input:
        input_one:
          default: val
    """

    entry = MagicMock()
    entry.domain = DOMAIN
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    with (
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=[]),
        patch.object(coordinator, "_get_entities_configs", return_value={}),
    ):
        risks = coordinator._detect_breaking_changes(old_content, new_content, {})

    assert len(risks) == 0
