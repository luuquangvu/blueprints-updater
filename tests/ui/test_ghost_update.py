"""Test for ghost update detection via semantic normalization."""

from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator

RAW_REMOTE_YAML = """
blueprint:
  name: Test Blueprint
  domain: automation
  input:
    target_notification:
      name: Target Notification
      selector:
        select:
          options:
            - label: Admin
              value: admin
            - label: User
              value: user
"""

ENRICHED_LOCAL_YAML = """
blueprint:
  name: Test Blueprint
  domain: automation
  input:
    target_notification:
      name: Target Notification
      selector:
        select:
          options:
            - label: Admin
              value: admin
            - label: User
              value: user
          multiple: false
          custom_value: false
          sort: false
"""


@pytest.mark.asyncio
async def test_semantic_normalization_parity(hass: HomeAssistant) -> None:
    """Test that raw and enriched YAMLs are semantically normalized to the same form."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    normalized_remote = BlueprintUpdateCoordinator._ensure_source_url(RAW_REMOTE_YAML, source_url)
    normalized_local = BlueprintUpdateCoordinator._ensure_source_url(
        ENRICHED_LOCAL_YAML, source_url
    )

    remote_dict = cast(dict[str, Any], yaml_util.parse_yaml(normalized_remote))
    local_dict = cast(dict[str, Any], yaml_util.parse_yaml(normalized_local))

    assert remote_dict == local_dict

    selector = remote_dict["blueprint"]["input"]["target_notification"]["selector"]["select"]
    assert selector["multiple"] is False
    assert selector["custom_value"] is False
    assert selector["sort"] is False


@pytest.mark.asyncio
async def test_list_expansion_normalization(hass: HomeAssistant) -> None:
    """Test that single values are normalized to lists where expected by schema."""
    raw_yaml = """
blueprint:
  name: List Test
  domain: automation
  input:
    my_entity:
      selector:
        entity:
          domain: light
"""
    source_url = "https://example.com/test.yaml"
    normalized = BlueprintUpdateCoordinator._ensure_source_url(raw_yaml, source_url)
    parsed = cast(dict[str, Any], yaml_util.parse_yaml(normalized))

    domain = parsed["blueprint"]["input"]["my_entity"]["selector"]["entity"]["domain"]
    assert domain == ["light"]


@pytest.mark.asyncio
async def test_normalization_error_handling(hass: HomeAssistant) -> None:
    """Test that normalization failures fall back to raw YAML gracefully."""
    invalid_yaml = """
blueprint:
  name: Invalid Test
  domain: 123
"""
    source_url = "https://example.com/invalid.yaml"

    normalized = BlueprintUpdateCoordinator._ensure_source_url(invalid_yaml, source_url)

    parsed = cast(dict[str, Any], yaml_util.parse_yaml(normalized))
    assert parsed["blueprint"]["source_url"] == source_url
    assert parsed["blueprint"]["domain"] == 123


@pytest.mark.asyncio
async def test_selector_option_order_invariance(hass: HomeAssistant) -> None:
    """Test that selector options in different orders do not trigger ghost updates."""
    local_yaml = """
blueprint:
  name: Smart Knob
  domain: automation
  input:
    device:
      name: Smart Knob Device
      description: The smart knob device.
      selector:
        text:
          multiple: false
          multiline: false
    light_entity:
      name: Light Entity
      description: The light to be controlled.
"""
    remote_yaml = """
blueprint:
  name: Smart Knob
  domain: automation
  input:
    device:
      name: Smart Knob Device
      description: The smart knob device.
      selector:
        text:
          multiline: false
          multiple: false
    light_entity:
      name: Light Entity
      description: The light to be controlled.
"""
    source_url = "https://github.com/user/repo/blob/main/smart_knob.yaml"

    hash_local = BlueprintUpdateCoordinator._hash_content(local_yaml, source_url)
    hash_remote = BlueprintUpdateCoordinator._hash_content(remote_yaml, source_url)
    assert hash_local == hash_remote

    norm_local = BlueprintUpdateCoordinator._ensure_source_url(local_yaml, source_url)
    norm_remote = BlueprintUpdateCoordinator._ensure_source_url(remote_yaml, source_url)
    assert norm_local == norm_remote
