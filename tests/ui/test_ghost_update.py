"""Test for ghost update detection via semantic normalization."""

from typing import Any, cast

import pytest
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.blueprint_validation import (
    ensure_source_url,
    hash_content,
)

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
            - value: admin
              label: Admin
            - value: user
              label: User
"""


@pytest.mark.asyncio
async def test_semantic_normalization_parity(hass: HomeAssistant) -> None:
    """Test that raw and enriched YAMLs are semantically normalized to the same form."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    normalized_remote = ensure_source_url(RAW_REMOTE_YAML, source_url)
    normalized_local = ensure_source_url(ENRICHED_LOCAL_YAML, source_url)

    remote_dict = cast(dict[str, Any], yaml_util.parse_yaml(normalized_remote))
    local_dict = cast(dict[str, Any], yaml_util.parse_yaml(normalized_local))

    assert remote_dict == local_dict


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
    normalized = ensure_source_url(raw_yaml, source_url)
    parsed = cast(dict[str, Any], yaml_util.parse_yaml(normalized))

    domain = parsed["blueprint"]["input"]["my_entity"]["selector"]["entity"]["domain"]
    assert domain == ["light"]


@pytest.mark.asyncio
async def test_normalization_error_handling(hass: HomeAssistant) -> None:
    """Test that normalization failures raise InvalidBlueprint instead of fallback."""
    invalid_yaml = """
blueprint:
  name: Invalid Test
  domain: 123
"""
    source_url = "https://example.com/invalid.yaml"

    with pytest.raises(InvalidBlueprint):
        ensure_source_url(invalid_yaml, source_url)


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

    hash_local = hash_content(local_yaml, source_url)
    hash_remote = hash_content(remote_yaml, source_url)
    assert hash_local == hash_remote

    norm_local = ensure_source_url(local_yaml, source_url, sort_selectors=True)
    norm_remote = ensure_source_url(remote_yaml, source_url, sort_selectors=True)
    assert norm_local == norm_remote


@pytest.mark.asyncio
async def test_entity_selector_filter_defaults_parity(hass: HomeAssistant) -> None:
    """Test that entity selectors with filters normalize defaults identically."""
    has_reorder = (
        hasattr(selector, "EntitySelectorConfig")
        and "reorder" in getattr(selector, "EntitySelectorConfig", {}).__annotations__
    )
    reorder_line = "          reorder: false\n" if has_reorder else ""
    local_yaml = f"""
blueprint:
  name: Camera Snapshot Full LLM
  domain: automation
  input:
    target_sensor:
      name: Target Sensor
      selector:
        entity:
          filter:
            - domain:
                - sensor
              integration: template
          multiple: false
{reorder_line}"""
    remote_yaml = """
blueprint:
  name: Camera Snapshot Full LLM
  domain: automation
  input:
    target_sensor:
      name: Target Sensor
      selector:
        entity:
          filter:
            - domain:
                - sensor
              integration: template
"""
    source_url = "https://github.com/user/repo/blob/main/camera_snapshot.yaml"

    hash_local = hash_content(local_yaml, source_url)
    hash_remote = hash_content(remote_yaml, source_url)
    assert hash_local == hash_remote

    norm_local = ensure_source_url(local_yaml, source_url, sort_selectors=True)
    norm_remote = ensure_source_url(remote_yaml, source_url, sort_selectors=True)
    assert norm_local == norm_remote


@pytest.mark.asyncio
async def test_recursive_selector_filter_key_sorting_parity(hass: HomeAssistant) -> None:
    """Test that nested filter keys ordered differently produce identical hashes."""
    yaml_variant_a = """
blueprint:
  name: Filter Sorting Test
  domain: automation
  input:
    target_entity:
      name: Target Entity
      selector:
        entity:
          filter:
            - domain: sensor
              integration: template
"""
    yaml_variant_b = """
blueprint:
  name: Filter Sorting Test
  domain: automation
  input:
    target_entity:
      name: Target Entity
      selector:
        entity:
          filter:
            - integration: template
              domain: sensor
"""
    source_url = "https://github.com/user/repo/blob/main/filter_sorting.yaml"

    hash_a = hash_content(yaml_variant_a, source_url)
    hash_b = hash_content(yaml_variant_b, source_url)
    assert hash_a == hash_b

    norm_a = ensure_source_url(yaml_variant_a, source_url, sort_selectors=True)
    norm_b = ensure_source_url(yaml_variant_b, source_url, sort_selectors=True)
    assert norm_a == norm_b


@pytest.mark.asyncio
async def test_empty_selector_none_normalization_stabilization(hass: HomeAssistant) -> None:
    """Test that empty selectors such as text: None stabilize identically across passes."""
    empty_selector_yaml = """
blueprint:
  name: Empty Selector Test
  domain: automation
  input:
    custom_text:
      name: Custom Text
      selector:
        text:
"""
    source_url = "https://github.com/user/repo/blob/main/empty_selector.yaml"

    pass1 = ensure_source_url(empty_selector_yaml, source_url)
    pass2 = ensure_source_url(pass1, source_url)
    assert pass1 == pass2

    hash_raw = hash_content(empty_selector_yaml, source_url)
    hash_pass1 = hash_content(pass1, source_url)
    hash_pass2 = hash_content(pass2, source_url)
    assert hash_raw == hash_pass1 == hash_pass2
