"""Tests for YAML stabilization idempotency, ghost update elimination, and list scoping."""

import copy
import hashlib
import inspect
import os
from collections.abc import ItemsView, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    import probatio as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

import yaml
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import yaml as yaml_util
from homeassistant.util.yaml.objects import Input

from custom_components.blueprints_updater.blueprint_validation import (
    BlueprintSchemaAdapter,
    _order_blueprint_metadata,
    _safe_deepcopy,
    _stabilize_dict_structure,
    canonicalize_source_url,
    ensure_source_url,
    ensure_source_url_cached,
    get_blueprint_canonical_keys,
    hash_content,
    normalize_content,
    set_cached_selector_filter_paths,
    stabilize_yaml_structure,
)
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)
from custom_components.blueprints_updater.file_store import FileTransactionResult
from custom_components.blueprints_updater.utils import get_max_backups

# Pattern 1: Blackshome Sensor Light pattern
# (filter as single dict expanded to list by schema inside selector)
SENSOR_LIGHT_PATTERN = """blueprint:
  name: Sensor Light
  description: Motion activated light automation with ambient light and dynamic lighting.
  domain: automation
  input:
    motion_sensor:
      name: Motion Sensor
      selector:
        entity:
          filter:
            domain: sensor
            device_class: illuminance
    ambient_light_sensor:
      name: Ambient Light Sensor
      selector:
        entity:
          filter:
            - domain: sensor
              device_class: illuminance
    bypass_switch:
      name: Bypass Switch
      selector:
        entity:
          filter:
            domain:
              - input_boolean
              - switch
trigger:
  - platform: state
    entity_id: binary_sensor.motion
action:
  - service: light.turn_on
"""

# Pattern 2: Blackshome Low Battery Notifications pattern
# (no source_url in raw YAML + complex selectors)
LOW_BATTERY_PATTERN = """blueprint:
  name: Low Battery Notifications & Actions
  description: Battery level notifications and helper trigger actions.
  domain: automation
  input:
    trigger_settings:
      name: Triggers
      input:
        button_entity:
          name: Button Helper
          default: []
          selector:
            entity:
              filter:
                domain: input_button
        battery_sensors:
          name: Battery Sensors
          selector:
            entity:
              filter:
                - device_class: battery
                  domain: sensor
              multiple: true
trigger:
  - platform: time
    at: "00:00:00"
action:
  - service: notify.notify
    data:
      message: Low battery alert
"""

# Pattern 3: Panhans Advanced Heating Control pattern
# (nested sections, multi-domain selectors, explicit source_url in raw)
ADVANCED_HEATING_PATTERN = """blueprint:
  name: Advanced Heating Control
  description: Multi-room heating scheduler and calibration control.
  domain: automation
  source_url: >-
    https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml
  input:
    thermostat_section:
      name: Thermostats & Sensors
      input:
        input_trvs:
          name: Thermostats / Climates
          selector:
            entity:
              filter:
                - domain:
                    - climate
              multiple: true
        input_temperature_sensor:
          name: Room Temperature Sensor
          default: []
          selector:
            entity:
              filter:
                - domain:
                    - sensor
                  device_class:
                    - temperature
              multiple: false
trigger:
  - platform: time
    at: "06:00:00"
action:
  - service: climate.set_temperature
    data:
      temperature: 21
"""

# Pattern 4: Target, Area, and Select Selector pattern
# (exercises target.entity, target.device, area.device, and select.options)
TARGET_AREA_SELECT_PATTERN = """blueprint:
  name: Target Area Select Blueprint
  domain: automation
  input:
    target_light:
      name: Target Lights
      selector:
        target:
          entity:
            domain: light
          device:
            integration: hue
    area_devices:
      name: Area Filter
      selector:
        area:
          device:
            manufacturer: Philips
    light_mode:
      name: Light Mode
      selector:
        select:
          options:
            - label: Night
              value: night
            - label: Day
              value: day
          mode: dropdown
trigger:
  - platform: state
    entity_id: binary_sensor.motion
action:
  - service: light.turn_on
    target:
      entity_id: light.living_room
"""

# Pattern 5: Non-selector mapping-to-list change (e.g. actions/triggers)
NON_SELECTOR_LIST_PATTERN = """blueprint:
  name: Action List Blueprint
  domain: automation
  input: {}
trigger:
  - platform: state
    entity_id: binary_sensor.motion
action:
  - service: light.turn_on
    target:
      entity_id: light.living_room
"""

# Pattern 6: Runtime discovered selector with non-selector variables
RUNTIME_DISCOVERED_PATTERN = """blueprint:
  name: Runtime Discovered Blueprint
  domain: automation
  input:
    test_input:
      name: Test Input
      selector:
        custom_runtime:
          custom_filter:
            domain: sensor
            device_class: motion
variables:
  non_selector_setting:
    custom_runtime:
      custom_filter:
        domain: sensor
        device_class: motion
trigger:
  - platform: state
    entity_id: sensor.test
action:
  - service: light.turn_on
"""

# Pattern 7: Tuya Smart Knob pattern
# (community.home-assistant.io forum topic with embedded raw GitHub source_url,
# !input tags, and action/number/select selectors)
TUYA_SMART_KNOB_FORUM_URL = (
    "https://community.home-assistant.io/t/"
    "zigbee2mqtt-control-light-entity-including-press-turn-with-tuya-moes-smart-knob-ers-10tzbvk-aa/787779"
)
TUYA_SMART_KNOB_PATTERN = """blueprint:
  name: "Control light entity with Tuya ERS-10TZBVK-AA Smart Knob (command mode) - v 1.1"
  description: >
    Blueprint to easily configure the **Tuya ERS-10TZBVK-AA Smart Knob** to
    control a light entity when integrated into Home Assistant using **Zigbee2MQTT**.
  source_url: https://raw.githubusercontent.com/TriggrHappy/blueprint_tuya_smart_knob/refs/heads/main/blueprint.yaml
  domain: automation
  input:
    mqtt_topic:
      name: MQTT Topic
      selector:
        text:
    light_entity:
      name: Light Entity
      selector:
        entity:
          filter:
            - domain: light
    long_press_action:
      name: Long Press Action
      default: []
      selector:
        action: {}
    brightness_multiplier:
      name: Brightness Step Multiplier
      default: 1
      selector:
        number:
          min: 1
          max: 5
          mode: slider
    step_multiplier:
      name: Color Temperature Step Multiplier
      default: 5
      selector:
        number:
          min: 1
          max: 10
          mode: slider
    kelvin_min:
      name: Minimum Color Temperature (Kelvin)
      default: 2000
      selector:
        number:
          min: 1000
          max: 6500
    kelvin_max:
      name: Maximum Color Temperature (Kelvin)
      default: 6500
      selector:
        number:
          min: 1000
          max: 6500
    min_brightness:
      name: Minimum Brightness (%)
      default: 1
      selector:
        number:
          min: 0
          max: 100
    max_brightness:
      name: Maximum Brightness (%)
      default: 100
      selector:
        number:
          min: 0
          max: 100
    light_transition:
      name: Light Transition
      default: 0.2
      selector:
        number:
          min: 0.0
          max: 4.0
          step: 0.1
          unit_of_measurement: seconds
    automation_mode:
      name: Automation Mode
      default: single
      selector:
        select:
          mode: dropdown
          options:
            - single
            - restart
            - queued
            - parallel
trigger:
  - platform: mqtt
    topic: !input mqtt_topic
action:
  - service: light.turn_on
    target:
      entity_id: !input light_entity
"""


@pytest.mark.parametrize(
    ("name", "raw_yaml", "source_url"),
    [
        (
            "sensor_light_with_url",
            SENSOR_LIGHT_PATTERN,
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238",
        ),
        (
            "low_battery_with_url",
            LOW_BATTERY_PATTERN,
            "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2",
        ),
        (
            "advanced_heating_with_url",
            ADVANCED_HEATING_PATTERN,
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
        ),
        (
            "target_area_select_with_url",
            TARGET_AREA_SELECT_PATTERN,
            "https://github.com/example/blueprints/blob/main/target_area.yaml",
        ),
        (
            "sensor_light_no_url",
            SENSOR_LIGHT_PATTERN,
            None,
        ),
        (
            "low_battery_no_url",
            LOW_BATTERY_PATTERN,
            None,
        ),
        (
            "target_area_select_no_url",
            TARGET_AREA_SELECT_PATTERN,
            None,
        ),
        (
            "non_selector_list",
            NON_SELECTOR_LIST_PATTERN,
            None,
        ),
        (
            "tuya_smart_knob_with_url",
            TUYA_SMART_KNOB_PATTERN,
            TUYA_SMART_KNOB_FORUM_URL,
        ),
        (
            "tuya_smart_knob_no_url",
            TUYA_SMART_KNOB_PATTERN,
            None,
        ),
    ],
)
def test_yaml_stabilization_idempotent_across_passes(
    name: str, raw_yaml: str, source_url: str | None
) -> None:
    """Test that stabilization is strictly idempotent on the first pass.

    Contract invariant:
    1. Re-normalizing stabilized content must produce identical text (pass1 == pass2 == pass3).
    2. Hashing stabilized content must remain stable across all subsequent passes.
    3. Hashing raw content with source URL canonicalization must match hashing stabilized content.
    """
    if source_url:
        canonical_source_url = canonicalize_source_url(source_url)
        pass1 = ensure_source_url_cached(raw_yaml, canonical_source_url)
        pass2 = ensure_source_url_cached(pass1, canonical_source_url)
        pass3 = ensure_source_url_cached(pass2, canonical_source_url)
    else:
        pass1 = normalize_content(raw_yaml)
        pass2 = normalize_content(pass1)
        pass3 = normalize_content(pass2)

    assert pass1 == pass2, f"Failed stabilization idempotency pass 1 vs 2 for {name}"
    assert pass2 == pass3, f"Failed stabilization idempotency pass 2 vs 3 for {name}"

    hash_pass1 = hash_content(pass1, source_url)
    hash_pass2 = hash_content(pass2, source_url)
    hash_pass3 = hash_content(pass3, source_url)

    assert hash_pass1 == hash_pass2 == hash_pass3, f"Hash instability across passes for {name}"

    # Semantic hash calculated directly from raw YAML matches hash from stabilized passes
    hash_from_raw = hash_content(raw_yaml, source_url)
    assert hash_from_raw == hash_pass1, f"Hash mismatch between raw and stabilized for {name}"

    # Verify that the stabilized output safely parses as valid YAML with core metadata preserved
    try:
        parsed_stabilized = yaml.safe_load(pass1)
    except yaml.YAMLError:
        parsed_stabilized = yaml_util.parse_yaml(pass1)
    try:
        parsed_raw = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        parsed_raw = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(parsed_stabilized, dict)
    assert isinstance(parsed_raw, dict)
    if isinstance(parsed_raw.get("blueprint"), dict) and isinstance(
        parsed_stabilized.get("blueprint"), dict
    ):
        assert parsed_stabilized["blueprint"].get("name") == parsed_raw["blueprint"].get("name")
        assert parsed_stabilized["blueprint"].get("domain") == parsed_raw["blueprint"].get("domain")


def test_transport_normalization_line_endings_and_bom() -> None:
    """Test that transport-level normalization strips BOM and standardizes CRLF line endings."""
    crlf_content = "blueprint:\r\n  name: Test\r\n  domain: automation\r\n"
    bom_content = "\ufeffblueprint:\n  name: Test\n  domain: automation\n"
    canonical_content = "blueprint:\n  name: Test\n  domain: automation\n"

    assert normalize_content(crlf_content) == canonical_content
    assert normalize_content(bom_content) == canonical_content
    assert normalize_content(canonical_content) == canonical_content

    # Hash invariance under transport variations
    assert (
        hash_content(crlf_content) == hash_content(bom_content) == hash_content(canonical_content)
    )


def test_source_url_injection_and_removal_idempotency() -> None:
    """Test source URL insertion idempotency independently from raw content normalization."""
    raw_content = "blueprint:\n  name: Test\n  domain: automation\n"
    url = "https://github.com/user/repo/blob/main/blueprint.yaml"
    canonical_url = canonicalize_source_url(url)

    injected_1 = ensure_source_url_cached(raw_content, canonical_url)
    injected_2 = ensure_source_url_cached(injected_1, canonical_url)
    assert injected_1 == injected_2
    assert "source_url:" in injected_1

    # Injected content hash matches hash_content with source_url
    assert hash_content(injected_1) == hash_content(raw_content, source_url=url)


def test_tuya_smart_knob_forum_pattern_stabilization() -> None:
    """Test YAML stabilization and semantic hashing for Tuya Smart Knob HA forum blueprint."""
    source_url = TUYA_SMART_KNOB_FORUM_URL
    canonical_url = canonicalize_source_url(source_url)
    assert canonical_url == "https://community.home-assistant.io/t/787779"

    stabilized_pass1 = ensure_source_url_cached(TUYA_SMART_KNOB_PATTERN, canonical_url)
    stabilized_pass2 = ensure_source_url_cached(stabilized_pass1, canonical_url)
    assert stabilized_pass1 == stabilized_pass2

    # Canonical forum source_url replaces embedded raw GitHub URL
    assert f"source_url: {canonical_url}" in stabilized_pass1

    # Semantic hashing is stable across raw and stabilized passes
    hash_raw = hash_content(TUYA_SMART_KNOB_PATTERN, source_url)
    hash_pass1 = hash_content(stabilized_pass1, source_url)
    hash_pass2 = hash_content(stabilized_pass2, source_url)
    assert hash_raw == hash_pass1 == hash_pass2

    # Ensure metadata integrity under yaml_util parsing
    parsed = yaml_util.parse_yaml(stabilized_pass1)
    assert isinstance(parsed, dict)
    assert (
        parsed["blueprint"]["name"]
        == "Control light entity with Tuya ERS-10TZBVK-AA Smart Knob (command mode) - v 1.1"
    )
    assert parsed["blueprint"]["domain"] == "automation"
    assert parsed["blueprint"]["source_url"] == canonical_url


def test_non_selector_mapping_to_list_not_coerced() -> None:
    """Test that non-selector dict-to-list structures are not coerced as singleton mappings."""
    # 1. Action mapping to list: key order from normalized list is preserved without orig alignment
    orig_action = {
        "target": {"entity_id": "light.kitchen"},
        "service": "light.turn_off",
        "data": {"brightness": 100},
    }
    norm_action = [{"service": "light.turn_on", "target": {"entity_id": "light.living_room"}}]

    stabilized = stabilize_yaml_structure(orig_action, norm_action, selector_path=None)
    assert stabilized == norm_action

    # 2. Non-selector dict with filter-like keys matching a selector filter path
    # but located outside selector scope (e.g. metadata/variables)
    orig_custom_filter = {"domain": "sensor", "device_class": "motion"}
    norm_custom_filter = [{"domain": ["sensor"], "device_class": ["motion"]}]

    stabilized_non_selector = stabilize_yaml_structure(
        orig_custom_filter,
        norm_custom_filter,
        selector_path=None,
        allow_singleton_list_coercion=False,
    )
    assert stabilized_non_selector == norm_custom_filter
    assert isinstance(stabilized_non_selector, list)
    assert stabilized_non_selector[0] == {"domain": ["sensor"], "device_class": ["motion"]}


def test_selector_mapping_to_list_coerced_and_sorted() -> None:
    """Test that selector mapping-to-list expansions preserve dict context and sort keys."""
    orig_filter = {"domain": "sensor", "device_class": "illuminance"}
    norm_filter = [{"domain": ["sensor"], "device_class": ["illuminance"]}]

    stabilized = stabilize_yaml_structure(
        orig_filter,
        norm_filter,
        selector_path=("entity", "filter"),
        allow_singleton_list_coercion=True,
    )
    assert isinstance(stabilized, list)
    assert len(stabilized) == 1
    # Full mapping check: keys sorted alphabetically and values preserved
    item = stabilized[0]
    assert isinstance(item, dict)
    assert list(item.keys()) == ["device_class", "domain"]
    assert stabilized == [{"device_class": ["illuminance"], "domain": ["sensor"]}]

    # Nested multiple selector keys case through automatic path discovery
    orig_nested = {
        "selector": {
            "entity": {
                "filter": {"domain": "sensor", "device_class": "illuminance"},
                "multiple": False,
            }
        }
    }
    norm_nested = {
        "selector": {
            "entity": {
                "filter": [{"domain": ["sensor"], "device_class": ["illuminance"]}],
                "multiple": False,
                "reorder": False,
            }
        }
    }
    stabilized_nested = stabilize_yaml_structure(orig_nested, norm_nested, selector_path=None)
    assert stabilized_nested == {
        "selector": {
            "entity": {
                "filter": [{"device_class": ["illuminance"], "domain": ["sensor"]}],
                "multiple": False,
            }
        }
    }

    # Full document entry point with selector_path=None (default entry point)
    orig_doc = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "input": {
            "motion_sensor": {
                "name": "Motion Sensor",
                "selector": {
                    "entity": {
                        "filter": {"domain": "sensor", "device_class": "illuminance"},
                    }
                },
            }
        },
    }
    norm_doc = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "input": {
            "motion_sensor": {
                "name": "Motion Sensor",
                "selector": {
                    "entity": {
                        "filter": [{"domain": ["sensor"], "device_class": ["illuminance"]}],
                        "multiple": False,
                    }
                },
            }
        },
    }
    stabilized_doc = stabilize_yaml_structure(orig_doc, norm_doc, selector_path=None)
    assert stabilized_doc == {
        "blueprint": {"name": "Test", "domain": "automation"},
        "input": {
            "motion_sensor": {
                "name": "Motion Sensor",
                "selector": {
                    "entity": {
                        "filter": [{"device_class": ["illuminance"], "domain": ["sensor"]}],
                    }
                },
            }
        },
    }

    # Already-list-valued multi-item selector filter case
    orig_multi_list_doc = {
        "blueprint": {"name": "Multi Filter Test", "domain": "automation"},
        "input": {
            "sensors": {
                "name": "Sensors",
                "selector": {
                    "entity": {
                        "filter": [
                            {"domain": "sensor", "device_class": "battery"},
                            {"domain": "binary_sensor", "device_class": "motion"},
                        ],
                    }
                },
            }
        },
    }
    norm_multi_list_doc = {
        "blueprint": {"name": "Multi Filter Test", "domain": "automation"},
        "input": {
            "sensors": {
                "name": "Sensors",
                "selector": {
                    "entity": {
                        "filter": [
                            {"domain": ["sensor"], "device_class": ["battery"]},
                            {"domain": ["binary_sensor"], "device_class": ["motion"]},
                        ],
                    }
                },
            }
        },
    }
    stabilized_multi_list = stabilize_yaml_structure(
        orig_multi_list_doc, norm_multi_list_doc, selector_path=None
    )
    assert stabilized_multi_list == {
        "blueprint": {"name": "Multi Filter Test", "domain": "automation"},
        "input": {
            "sensors": {
                "name": "Sensors",
                "selector": {
                    "entity": {
                        "filter": [
                            {"device_class": ["battery"], "domain": ["sensor"]},
                            {"device_class": ["motion"], "domain": ["binary_sensor"]},
                        ],
                    }
                },
            }
        },
    }


def test_select_selector_options_stabilization() -> None:
    """Test that select selector options with label/value maps preserve order and sort keys."""
    raw_select = {
        "select": {
            "options": [
                {"value": "val2", "label": "Label 2"},
                {"value": "val1", "label": "Label 1"},
            ],
            "mode": "dropdown",
        }
    }
    norm_select = {
        "select": {
            "options": [
                {"value": "val2", "label": "Label 2"},
                {"value": "val1", "label": "Label 1"},
            ],
            "mode": "dropdown",
            "multiple": False,
            "custom_value": False,
            "sort": False,
        }
    }
    stabilized = stabilize_yaml_structure(raw_select, norm_select, selector_path=())
    assert stabilized == {
        "select": {
            "mode": "dropdown",
            "options": [
                {"label": "Label 2", "value": "val2"},
                {"label": "Label 1", "value": "val1"},
            ],
        }
    }


def test_default_selector_filter_paths_contract() -> None:
    """Test that _DEFAULT_SELECTOR_FILTER_PATHS defines expected contract."""
    expected_defaults = frozenset(
        {
            ("entity", "filter"),
            ("device", "filter"),
            ("device", "entity"),
            ("target", "entity"),
            ("target", "device"),
            ("area", "entity"),
            ("area", "device"),
            ("floor", "entity"),
            ("floor", "device"),
            ("numeric_threshold", "entity"),
        }
    )
    defaults = BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS
    assert isinstance(defaults, frozenset)
    assert defaults == expected_defaults


def test_compute_selector_registry_fingerprint_invariance_and_sensitivity() -> None:
    """Test selector registry fingerprinting is deterministic, order-invariant, and sensitive."""
    from custom_components.blueprints_updater.blueprint_validation import (
        compute_selector_registry_fingerprint,
    )

    class MockSelectorA:
        """Mock selector class A."""

        CONFIG_SCHEMA = vol.Schema({vol.Optional("field_a"): str})

    class MockSelectorB:
        """Mock selector class B."""

        CONFIG_SCHEMA = vol.Schema({vol.Optional("field_b"): int})

    # 1. Non-mapping inputs return None
    assert compute_selector_registry_fingerprint(None) is None
    assert compute_selector_registry_fingerprint(12345) is None

    # 2. Key ordering invariance (dictionary insertion order does not affect fingerprint)
    reg_1 = {"selector_a": MockSelectorA, "selector_b": MockSelectorB}
    reg_2 = {"selector_b": MockSelectorB, "selector_a": MockSelectorA}
    fp_1 = compute_selector_registry_fingerprint(reg_1)
    fp_2 = compute_selector_registry_fingerprint(reg_2)
    assert fp_1 is not None
    assert fp_1 == fp_2

    # 3. Sensitivity to registry key change
    reg_key_changed = {"selector_renamed": MockSelectorA, "selector_b": MockSelectorB}
    fp_key_changed = compute_selector_registry_fingerprint(reg_key_changed)
    assert fp_key_changed != fp_1

    # 4. Sensitivity to selector class change
    reg_cls_changed = {"selector_a": MockSelectorB, "selector_b": MockSelectorB}
    fp_cls_changed = compute_selector_registry_fingerprint(reg_cls_changed)
    assert fp_cls_changed != fp_1

    # 5. Sensitivity to schema mutation
    orig_schema = MockSelectorA.CONFIG_SCHEMA
    try:
        MockSelectorA.CONFIG_SCHEMA = vol.Schema({vol.Optional("field_a"): int})
        fp_schema_changed = compute_selector_registry_fingerprint(reg_1)
        assert fp_schema_changed != fp_1
    finally:
        MockSelectorA.CONFIG_SCHEMA = orig_schema


@pytest.fixture
def reset_selector_cache() -> Iterator[None]:
    """Save and restore coordinator selector cache and fingerprint state."""
    orig_cache = BlueprintUpdateCoordinator._selector_filter_paths
    orig_fingerprint = BlueprintUpdateCoordinator._selector_registry_fingerprint
    BlueprintUpdateCoordinator.invalidate_selector_cache()
    try:
        yield
    finally:
        BlueprintUpdateCoordinator._selector_filter_paths = orig_cache
        BlueprintUpdateCoordinator._selector_registry_fingerprint = orig_fingerprint
        set_cached_selector_filter_paths(orig_cache, orig_fingerprint)


def test_dynamic_selector_discovery_and_caching(reset_selector_cache: None) -> None:
    """Test dynamic selector discovery and caching with a mock registry."""

    class CustomSelectorEligible:
        """Custom eligible selector with filter list expansion."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("filter"): vol.All(
                    cv.ensure_list, [vol.Schema({"domain": str, "device_class": str})]
                )
            }
        )

    class CustomSelectorComposed:
        """Custom eligible selector with composed vol.Any and vol.All wrapping."""

        CONFIG_SCHEMA = vol.All(
            vol.Schema(
                {
                    vol.Optional("composed_filter"): vol.Any(
                        vol.All(cv.ensure_list, [vol.Schema({"integration": str})])
                    )
                }
            )
        )

    class CustomSelectorIneligible:
        """Custom ineligible selector without filter list expansion."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("options"): [str],
                vol.Optional("static_map"): {"key": str},
            }
        )

    class MalformedMeta(type):
        """Metaclass raising on attribute access."""

        @property
        def CONFIG_SCHEMA(cls) -> None:  # noqa: N802
            """Raise error on class schema access."""
            raise RuntimeError("Broken selector schema")

    class CustomSelectorMalformed(metaclass=MalformedMeta):
        """Custom selector with a raising class property."""

    class DynamicSchemaMeta(type):
        """Metaclass constructing a fresh Schema instance on each access."""

        @property
        def CONFIG_SCHEMA(cls) -> vol.Schema:  # noqa: N802
            """Generate fresh schema on each property access."""
            return vol.Schema(
                {
                    vol.Optional("dynamic_filter"): vol.All(
                        cv.ensure_list, [vol.Schema({"domain": str})]
                    )
                }
            )

    class CustomSelectorDynamic(metaclass=DynamicSchemaMeta):
        """Custom selector with dynamically evaluated schema."""

    class CustomSelectorNested:
        """Custom selector with a nested schema filter path."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("nested_group"): vol.Schema(
                    {
                        vol.Optional("sub_filter"): vol.All(
                            cv.ensure_list, [vol.Schema({"domain": str})]
                        )
                    }
                )
            }
        )

    class CustomSelectorMultiBranch:
        """Custom selector with multi-branch vol.All / vol.Any schemas."""

        CONFIG_SCHEMA = vol.All(
            vol.Schema({"initial_field": str}),
            vol.Schema(
                {
                    vol.Optional("second_branch_filter"): vol.All(
                        cv.ensure_list, [vol.Schema({"integration": str})]
                    )
                }
            ),
        )

    mock_registry: dict[str, type] = {
        "custom_eligible": CustomSelectorEligible,
        "custom_composed": CustomSelectorComposed,
        "custom_ineligible": CustomSelectorIneligible,
        "custom_malformed": CustomSelectorMalformed,
        "custom_dynamic": CustomSelectorDynamic,
        "custom_nested": CustomSelectorNested,
        "custom_multibranch": CustomSelectorMultiBranch,
    }

    # Verify fresh schema instances have distinct object identities across accesses
    dynamic_schema_1 = CustomSelectorDynamic.CONFIG_SCHEMA
    dynamic_schema_2 = CustomSelectorDynamic.CONFIG_SCHEMA
    assert dynamic_schema_1 is not dynamic_schema_2

    with patch("homeassistant.helpers.selector.SELECTORS", mock_registry):
        discovered = BlueprintUpdateCoordinator.get_selector_filter_paths(force_refresh=True)
        assert ("custom_eligible", "filter") in discovered
        assert ("custom_composed", "composed_filter") in discovered
        assert ("custom_dynamic", "dynamic_filter") in discovered
        assert ("custom_nested", "nested_group", "sub_filter") in discovered
        assert ("custom_multibranch", "second_branch_filter") in discovered
        assert ("custom_ineligible", "options") not in discovered
        assert ("custom_ineligible", "static_map") not in discovered
        assert all(p[0] != "custom_malformed" for p in discovered)

        # Unchanged lookups return the exact cached object despite dynamic schema instantiation
        cached_lookup = BlueprintUpdateCoordinator.get_selector_filter_paths()
        assert cached_lookup is discovered


def test_dynamic_selector_fingerprint_sensitivity_and_mutation(
    reset_selector_cache: None,
) -> None:
    """Test dynamic selector cache invalidation across schema mutations and replacements."""

    class CustomSelectorEligible:
        """Custom eligible selector with filter list expansion."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("filter"): vol.All(
                    cv.ensure_list, [vol.Schema({"domain": str, "device_class": str})]
                )
            }
        )

    class CustomSelectorReplaced:
        """Replacement selector with a different filter field."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("device_filter"): vol.All(
                    cv.ensure_list, [vol.Schema({"integration": str})]
                )
            }
        )

    class CustomSelectorNested:
        """Custom selector with a nested schema filter path."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("nested_group"): vol.Schema(
                    {
                        vol.Optional("sub_filter"): vol.All(
                            cv.ensure_list, [vol.Schema({"domain": str})]
                        )
                    }
                )
            }
        )

    mock_registry: dict[str, type] = {
        "custom_eligible": CustomSelectorEligible,
        "custom_nested": CustomSelectorNested,
    }

    orig_replaced_schema = dict(CustomSelectorReplaced.CONFIG_SCHEMA.schema)
    nested_group_obj = CustomSelectorNested.CONFIG_SCHEMA.schema.get(vol.Optional("nested_group"))
    assert isinstance(nested_group_obj, vol.Schema)
    assert isinstance(nested_group_obj.schema, dict)
    orig_nested_schema = dict(nested_group_obj.schema)

    try:
        with patch("homeassistant.helpers.selector.SELECTORS", mock_registry):
            discovered = BlueprintUpdateCoordinator.get_selector_filter_paths(force_refresh=True)
            assert ("custom_eligible", "filter") in discovered
            assert ("custom_nested", "nested_group", "sub_filter") in discovered

            # Replacing a selector under existing key should invalidate cache via fingerprint
            mock_registry["custom_eligible"] = CustomSelectorReplaced
            updated = BlueprintUpdateCoordinator.get_selector_filter_paths()
            assert ("custom_eligible", "device_filter") in updated

            # In-place schema mutation on existing selector invalidates cache automatically
            assert isinstance(CustomSelectorReplaced.CONFIG_SCHEMA.schema, dict)
            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("mutated_filter")] = vol.All(
                cv.ensure_list, [vol.Schema({"target": str})]
            )
            mutated_updated = BlueprintUpdateCoordinator.get_selector_filter_paths()
            assert ("custom_eligible", "mutated_filter") in mutated_updated

            # In-place nested schema mutation invalidates cache automatically via
            # recursive fingerprint
            nested_group_schema = CustomSelectorNested.CONFIG_SCHEMA.schema.get(
                vol.Optional("nested_group")
            )
            assert isinstance(nested_group_schema, vol.Schema)
            assert isinstance(nested_group_schema.schema, dict)
            nested_group_schema.schema[vol.Optional("extra_sub_filter")] = vol.All(
                cv.ensure_list, [vol.Schema({"domain": str})]
            )
            nested_updated = BlueprintUpdateCoordinator.get_selector_filter_paths()
            assert ("custom_nested", "nested_group", "extra_sub_filter") in nested_updated

            # In-place removal of an expandable schema field invalidates cache automatically
            del nested_group_schema.schema[vol.Optional("extra_sub_filter")]
            nested_removed_updated = BlueprintUpdateCoordinator.get_selector_filter_paths()
            assert (
                "custom_nested",
                "nested_group",
                "extra_sub_filter",
            ) not in nested_removed_updated

            # In-place scalar/metadata mutation without changing filter paths
            # invalidates fingerprint
            fp_before = BlueprintUpdateCoordinator._selector_registry_fingerprint
            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("non_path_scalar")] = int
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_after = BlueprintUpdateCoordinator._selector_registry_fingerprint
            assert fp_before != fp_after

            # Callable closure state mutation without changing filter paths
            # invalidates fingerprint
            def make_closure_validator(bound_val: int) -> object:
                """Create validator closure capturing a bound value."""

                def closure_fn(v: object) -> object:
                    """Closure validator returning formatted string."""
                    return f"{v}_{bound_val}"

                return closure_fn

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("closure_field")] = (
                make_closure_validator(42)
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_closure_before = BlueprintUpdateCoordinator._selector_registry_fingerprint

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("closure_field")] = (
                make_closure_validator(100)
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_closure_after = BlueprintUpdateCoordinator._selector_registry_fingerprint
            assert fp_closure_before != fp_closure_after

            # Callable defaults and kwdefaults mutation invalidates fingerprint
            def make_default_fn(default_arg: int = 10, *, kw_arg: str = "init") -> object:
                """Create function with test defaults and kwdefaults."""

                def sample_fn(v: object = default_arg, *, k: str = kw_arg) -> object:
                    """Sample function returning combined arguments."""
                    return f"{v}_{k}"

                return sample_fn

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("default_field")] = (
                make_default_fn(10, kw_arg="init")
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_default_before = BlueprintUpdateCoordinator._selector_registry_fingerprint

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("default_field")] = (
                make_default_fn(20, kw_arg="mutated")
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_default_after = BlueprintUpdateCoordinator._selector_registry_fingerprint
            assert fp_default_before != fp_default_after

            # Opaque non-callable object state mutation without changing filter paths
            # invalidates fingerprint
            class OpaqueValidator:
                """Mock opaque non-callable validator with instance state."""

                def __init__(self, token: str) -> None:
                    """Initialize opaque validator with test token."""
                    self.token = token

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("opaque_field")] = (
                OpaqueValidator("token_a")
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_opaque_before = BlueprintUpdateCoordinator._selector_registry_fingerprint

            CustomSelectorReplaced.CONFIG_SCHEMA.schema[vol.Optional("opaque_field")] = (
                OpaqueValidator("token_b")
            )
            BlueprintUpdateCoordinator.get_selector_filter_paths()
            fp_opaque_after = BlueprintUpdateCoordinator._selector_registry_fingerprint
            assert fp_opaque_before != fp_opaque_after

    finally:
        CustomSelectorReplaced.CONFIG_SCHEMA.schema = dict(orig_replaced_schema)
        nested_group_cleanup = CustomSelectorNested.CONFIG_SCHEMA.schema.get(
            vol.Optional("nested_group")
        )
        if isinstance(nested_group_cleanup, vol.Schema) and isinstance(
            nested_group_cleanup.schema, dict
        ):
            nested_group_cleanup.schema = dict(orig_nested_schema)


def test_dynamic_selector_discovery_failure_tolerance(
    reset_selector_cache: None,
) -> None:
    """Test dynamic selector discovery failure-tolerance and graceful fallbacks."""
    # Empty registry should safely preserve defaults without raising
    with patch("homeassistant.helpers.selector.SELECTORS", {}):
        empty_discovered = BlueprintUpdateCoordinator.get_selector_filter_paths(force_refresh=True)
        assert empty_discovered == BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS

    # Transitioning registry to None should automatically invalidate cache
    # without requiring force_refresh
    with patch("homeassistant.helpers.selector.SELECTORS", None):
        none_discovered = BlueprintUpdateCoordinator.get_selector_filter_paths()
        assert none_discovered == BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS

    # Module import failure should safely preserve defaults
    with patch.dict("sys.modules", {"homeassistant.helpers.selector": None}):
        import_failed_discovered = BlueprintUpdateCoordinator._derive_selector_filter_paths()
        assert import_failed_discovered == BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS

    # Malformed registry raising during items() access should safely preserve defaults
    class RaisingRegistry(Mapping[str, type]):
        """Mock registry raising on items()."""

        def __getitem__(self, key: str) -> type:
            """Raise error on item access."""
            raise RuntimeError("Registry item access failed")

        def __len__(self) -> int:
            """Return dummy count."""
            return 1

        def __iter__(self) -> Iterator[str]:
            """Raise error during iteration."""
            raise RuntimeError("Registry iteration failed")

        def items(self) -> ItemsView[str, type]:
            """Raise error during iteration."""
            raise RuntimeError("Registry iteration failed")

    with patch("homeassistant.helpers.selector.SELECTORS", RaisingRegistry()):
        raising_discovered = BlueprintUpdateCoordinator.get_selector_filter_paths(
            force_refresh=True
        )
        assert raising_discovered == BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS

    # Selector class raising during CONFIG_SCHEMA inspection should safely preserve defaults
    class BrokenSchemaDescriptor:
        """Descriptor raising on attribute access."""

        def __get__(self, obj: object, objtype: type | None = None) -> object:
            """Raise on access."""
            raise RuntimeError("Corrupt schema property")

    class BrokenSchemaSelector:
        """Mock selector whose CONFIG_SCHEMA raises on access."""

        CONFIG_SCHEMA = BrokenSchemaDescriptor()

    with patch(
        "homeassistant.helpers.selector.SELECTORS",
        {"broken_selector": BrokenSchemaSelector},
    ):
        broken_discovered = BlueprintUpdateCoordinator.get_selector_filter_paths(force_refresh=True)
        assert broken_discovered == BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS


def _build_dynamic_discovery_test_documents() -> tuple[dict[str, object], dict[str, object]]:
    """Construct paired raw and normalized test documents for dynamic discovery traversal."""
    raw_doc: dict[str, object] = {
        "blueprint": {
            "name": "Dynamic Discovery Blueprint",
            "input": {
                "expand_input": {
                    "selector": {
                        "custom_expand": {
                            "custom_filter": {"domain": "sensor", "device_class": "motion"}
                        }
                    }
                },
                "no_expand_input": {
                    "selector": {
                        "custom_no_expand": {
                            "custom_filter": {"domain": "sensor", "device_class": "motion"}
                        }
                    }
                },
                "no_expand_list_input": {
                    "selector": {
                        "custom_no_expand": {
                            "custom_filter": {"domain": "sensor", "device_class": "motion"}
                        }
                    }
                },
            },
        }
    }
    norm_doc: dict[str, object] = {
        "blueprint": {
            "name": "Dynamic Discovery Blueprint",
            "input": {
                "expand_input": {
                    "selector": {
                        "custom_expand": {
                            "custom_filter": [{"domain": ["sensor"], "device_class": ["motion"]}]
                        }
                    }
                },
                "no_expand_input": {
                    "selector": {
                        "custom_no_expand": {
                            "custom_filter": {"domain": "sensor", "device_class": "motion"}
                        }
                    }
                },
                "no_expand_list_input": {
                    "selector": {
                        "custom_no_expand": {
                            "custom_filter": [{"domain": ["sensor"], "device_class": ["motion"]}]
                        }
                    }
                },
            },
        }
    }
    return raw_doc, norm_doc


def _extract_selector_filter_from_yaml(
    yaml_text: str, input_name: str, selector_type: str, filter_field: str
) -> object:
    """Extract a selector filter value from parsed YAML content."""
    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict) or "blueprint" not in parsed:
        return None
    bp_dict = parsed["blueprint"]
    if not isinstance(bp_dict, dict) or "input" not in bp_dict:
        return None
    input_dict = bp_dict["input"]
    if not isinstance(input_dict, dict) or input_name not in input_dict:
        return None
    field_dict = input_dict[input_name]
    if not isinstance(field_dict, dict) or "selector" not in field_dict:
        return None
    selector_map = field_dict["selector"]
    if not isinstance(selector_map, dict) or selector_type not in selector_map:
        return None
    type_map = selector_map[selector_type]
    return type_map.get(filter_field) if isinstance(type_map, dict) else None


class _DiscoverySelectorExpand:
    """Mock selector supporting filter list expansion."""

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Optional("custom_filter"): vol.All(
                cv.ensure_list, [vol.Schema({"domain": str, "device_class": str})]
            )
        }
    )


class _DiscoverySelectorNoExpand:
    """Mock selector without filter list expansion."""

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Optional("custom_filter"): vol.Schema({"domain": str, "device_class": str}),
        }
    )


class _PipelineRuntimeSelector:
    """Mock runtime selector with expandable filter for pipeline tests."""

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Optional("custom_filter"): vol.All(
                cv.ensure_list, [vol.Schema({"domain": str, "device_class": str})]
            )
        }
    )


def _assert_dynamic_discovery_coercion(
    raw_doc: dict[str, object], norm_doc: dict[str, object]
) -> None:
    """Verify coercion of expandable selectors and preservation of non-expandable structures."""
    stabilized = stabilize_yaml_structure(raw_doc, norm_doc, selector_path=None)
    assert isinstance(stabilized, dict)

    # 1. Discovered custom_expand MUST be coerced from mapping to 1-item list with sorted keys
    expand_input = stabilized["blueprint"]["input"]["expand_input"]["selector"]["custom_expand"]
    assert expand_input["custom_filter"] == [{"device_class": ["motion"], "domain": ["sensor"]}]

    # 2. Non-expandable selector mapping remains a dictionary (NOT converted to list)
    no_expand_val = stabilized["blueprint"]["input"]["no_expand_input"]["selector"][
        "custom_no_expand"
    ]["custom_filter"]
    assert isinstance(no_expand_val, dict)
    assert not isinstance(no_expand_val, list)
    assert no_expand_val == {"device_class": "motion", "domain": "sensor"}

    # 3. Non-expandable selector with uncoerced list preserves list element key order
    assert stabilized["blueprint"]["input"]["no_expand_list_input"]["selector"]["custom_no_expand"][
        "custom_filter"
    ] == [{"domain": ["sensor"], "device_class": ["motion"]}]


def _assert_runtime_custom_selector_coerced(raw_yaml: str, source_url: str) -> None:
    """Verify custom selector expands to 1-item list inside selector and not in variables."""
    normalized = ensure_source_url(raw_yaml, source_url)

    # Selector custom_filter MUST be coerced to 1-item list with sorted keys
    selector_val = _extract_selector_filter_from_yaml(
        normalized, "test_input", "custom_runtime", "custom_filter"
    )
    assert isinstance(selector_val, list)
    assert len(selector_val) == 1
    assert selector_val[0] == {"device_class": "motion", "domain": "sensor"}

    # Non-selector custom_filter must NOT be coerced to list
    parsed = yaml.safe_load(normalized)
    non_selector_val = parsed["variables"]["non_selector_setting"]["custom_runtime"][
        "custom_filter"
    ]
    assert isinstance(non_selector_val, dict)
    assert not isinstance(non_selector_val, list)
    assert non_selector_val == {"domain": "sensor", "device_class": "motion"}


def _assert_runtime_custom_selector_uncoerced(raw_yaml: str, source_url: str) -> None:
    """Verify removed runtime custom selector preserves raw mapping representation."""
    normalized_removed = ensure_source_url(raw_yaml, source_url)
    selector_val_removed = _extract_selector_filter_from_yaml(
        normalized_removed, "test_input", "custom_runtime", "custom_filter"
    )
    assert isinstance(selector_val_removed, dict)
    assert not isinstance(selector_val_removed, list)
    assert selector_val_removed == {"domain": "sensor", "device_class": "motion"}


def test_dynamic_selector_coercion_in_document_traversal(
    reset_selector_cache: None,
) -> None:
    """Test dynamic selector coercion and rejection during full document traversal."""
    mock_registry: dict[str, type] = {
        "custom_expand": _DiscoverySelectorExpand,
        "custom_no_expand": _DiscoverySelectorNoExpand,
    }

    with patch("homeassistant.helpers.selector.SELECTORS", mock_registry):
        raw_doc, norm_doc = _build_dynamic_discovery_test_documents()
        _assert_dynamic_discovery_coercion(raw_doc, norm_doc)


def test_runtime_discovered_custom_selector_full_normalization_pipeline(
    reset_selector_cache: None,
) -> None:
    """Test that full document normalization pipeline coerces runtime discovered selector paths."""
    from homeassistant.helpers import selector as ha_selector

    mock_registry: dict[str, type] = {
        **ha_selector.SELECTORS,
        "custom_runtime": _PipelineRuntimeSelector,
    }

    source_url = "https://example.com/runtime_blueprint.yaml"

    with patch("homeassistant.helpers.selector.SELECTORS", mock_registry):
        _assert_runtime_custom_selector_coerced(RUNTIME_DISCOVERED_PATTERN, source_url)

    # Registry removal phase: remove custom_runtime and normalize again
    mock_registry_removed = {k: v for k, v in mock_registry.items() if k != "custom_runtime"}
    with patch("homeassistant.helpers.selector.SELECTORS", mock_registry_removed):
        _assert_runtime_custom_selector_uncoerced(RUNTIME_DISCOVERED_PATTERN, source_url)


def test_coercion_path_scoped_in_single_traversal() -> None:
    """Test that singleton coercion applies only to selector paths in document traversal."""
    raw_doc = {
        "blueprint": {
            "name": "Mixed Scope Blueprint",
            "input": {
                "motion": {
                    "selector": {
                        "entity": {
                            "filter": {"domain": "sensor", "device_class": "motion"},
                        }
                    }
                }
            },
        },
        "metadata": {
            "entity": {
                "filter": {"domain": "sensor", "device_class": "motion"},
            }
        },
        "action": {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
    }
    norm_doc = {
        "blueprint": {
            "name": "Mixed Scope Blueprint",
            "input": {
                "motion": {
                    "selector": {
                        "entity": {
                            "filter": [{"domain": ["sensor"], "device_class": ["motion"]}],
                        }
                    }
                }
            },
        },
        "metadata": {
            "entity": {
                "filter": [{"domain": ["sensor"], "device_class": ["motion"]}],
            }
        },
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}],
    }

    stabilized = stabilize_yaml_structure(raw_doc, norm_doc, selector_path=None)

    assert isinstance(stabilized, dict)
    assert stabilized == {
        "blueprint": {
            "name": "Mixed Scope Blueprint",
            "input": {
                "motion": {
                    "selector": {
                        "entity": {
                            "filter": [{"device_class": ["motion"], "domain": ["sensor"]}],
                        }
                    }
                }
            },
        },
        "metadata": {
            "entity": {
                "filter": [{"domain": ["sensor"], "device_class": ["motion"]}],
            }
        },
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}],
    }


@pytest.mark.parametrize(
    "path_tuple",
    sorted(BlueprintUpdateCoordinator._DEFAULT_SELECTOR_FILTER_PATHS),
)
def test_all_selector_filter_paths_coerced_and_sorted(path_tuple: tuple[str, ...]) -> None:
    """Test that all default selector filter paths coerce a singleton dict and sort keys."""
    selector_type, field_name = path_tuple
    raw_doc = {
        "blueprint": {
            "name": "Test Blueprint",
            "input": {
                "test_input": {
                    "selector": {
                        selector_type: {
                            field_name: {"z_key": "val1", "a_key": "val2"},
                        }
                    }
                }
            },
        }
    }
    norm_doc = {
        "blueprint": {
            "name": "Test Blueprint",
            "input": {
                "test_input": {
                    "selector": {
                        selector_type: {
                            field_name: [{"z_key": ["val1"], "a_key": ["val2"]}],
                        }
                    }
                }
            },
        }
    }
    stabilized = stabilize_yaml_structure(raw_doc, norm_doc, selector_path=None)
    assert isinstance(stabilized, dict)
    assert stabilized["blueprint"]["input"]["test_input"]["selector"][selector_type][
        field_name
    ] == [{"a_key": ["val2"], "z_key": ["val1"]}]


@pytest.mark.parametrize(
    ("orig_data", "norm_data", "allow_coercion", "expected"),
    [
        # Non-dict original: string with list
        ("sensor", [{"domain": ["sensor"]}], True, [{"domain": ["sensor"]}]),
        # Non-dict original with normalized mapping (schema-injected mapping)
        (
            "unexpected_scalar",
            {"domain": ["sensor"], "device_class": ["illuminance"]},
            True,
            {"device_class": ["illuminance"], "domain": ["sensor"]},
        ),
        # Empty normalized list
        ({"domain": "sensor"}, [], True, []),
        # Multi-item normalized list (not a singleton expansion)
        (
            {"domain": "sensor"},
            [{"domain": ["sensor"]}, {"domain": ["binary_sensor"]}],
            True,
            [{"domain": ["sensor"]}, {"domain": ["binary_sensor"]}],
        ),
        # Multi-item original list with multi-item normalized list
        (
            [{"z_key": "val1"}, {"a_key": "val2"}],
            [{"z_key": "val1"}, {"a_key": "val2"}],
            True,
            [{"z_key": "val1"}, {"a_key": "val2"}],
        ),
        # Multi-item normalized list with non-dict elements
        (["item1", "item2"], ["item1", "item2"], True, ["item1", "item2"]),
        # Non-dict singleton normalized list with dict original
        ({"key": "val"}, [123], True, [123]),
        # Non-dict singleton normalized list with string original
        ({"domain": "sensor"}, ["sensor"], True, ["sensor"]),
        # Disjoint keys mapping with allow_coercion=False (e.g. non-filter selector key)
        (
            {"z_key": "val", "a_key": "val"},
            [{"b_key": ["sensor"], "a_key": ["sensor"]}],
            False,
            [{"a_key": ["sensor"], "b_key": ["sensor"]}],
        ),
        # Disjoint keys mapping with allow_coercion=True
        (
            {"other_key": "val"},
            [{"domain": ["sensor"]}],
            True,
            [{"domain": ["sensor"]}],
        ),
        # Overlapping and extra keys (extra key dropped, normalized mapping preserved)
        (
            {"domain": "sensor", "extra": "value"},
            [{"domain": ["sensor"]}],
            True,
            [{"domain": ["sensor"]}],
        ),
        # Empty original mapping (schema defaults injected)
        ({}, [{"domain": ["sensor"]}], True, [{"domain": ["sensor"]}]),
        # Empty original and empty normalized mapping
        ({}, [{}], True, [{}]),
        # Single-item original list with matching singleton normalized list
        (
            [{"domain": "sensor", "device_class": "motion"}],
            [{"domain": ["sensor"], "device_class": ["motion"]}],
            True,
            [{"domain": ["sensor"], "device_class": ["motion"]}],
        ),
        # Length mismatch: multi-item original list with 1-item normalized list
        (
            [{"domain": "sensor"}, {"domain": "binary_sensor"}],
            [{"domain": ["sensor"]}],
            True,
            [{"domain": ["sensor"]}],
        ),
        # Length mismatch: 1-item original list with multi-item normalized list
        (
            [{"domain": "sensor"}],
            [{"domain": ["sensor"]}, {"domain": ["binary_sensor"]}],
            True,
            [{"domain": ["sensor"]}, {"domain": ["binary_sensor"]}],
        ),
    ],
)
def test_selector_list_coercion_boundaries(
    orig_data: object, norm_data: object, allow_coercion: bool, expected: object
) -> None:
    """Test boundary conditions to ensure only genuine singleton selector mappings are coerced."""
    stabilized = stabilize_yaml_structure(
        orig_data,
        norm_data,
        selector_path=("entity", "filter") if allow_coercion else ("entity",),
        allow_singleton_list_coercion=allow_coercion,
    )
    assert stabilized == expected


@pytest.mark.parametrize(
    ("url1", "url2", "should_match"),
    [
        (
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238",
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238/raw",
            True,
        ),
        (
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
            "https://raw.githubusercontent.com/panhans/HomeAssistant/main/blueprints/automation/panhans/advanced_heating_control.yaml",
            True,
        ),
        (
            "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2#file-battery-yaml",
            "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2",
            True,
        ),
        # Negative cases: distinct gists/files must produce different hashes
        (
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238",
            "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2",
            False,
        ),
        (
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/other.yaml",
            False,
        ),
        (
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
            "https://github.com/panhans/HomeAssistant/blob/dev/blueprints/automation/panhans/advanced_heating_control.yaml",
            False,
        ),
    ],
)
def test_hash_content_source_url_equivalence(url1: str, url2: str, should_match: bool) -> None:
    """Test that equivalent URLs match hashes, and distinct URLs produce different hashes."""
    content = SENSOR_LIGHT_PATTERN
    hash1 = hash_content(content, url1)
    hash2 = hash_content(content, url2)
    if should_match:
        assert hash1 == hash2
    else:
        assert hash1 != hash2

    # Verify that changing YAML content produces a different hash for the identical URL
    content_alt = LOW_BATTERY_PATTERN
    hash_alt = hash_content(content_alt, url1)
    assert hash1 != hash_alt


@pytest.mark.asyncio
async def test_prepare_blueprint_install_hash_validation(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that _prepare_blueprint_install validates hashes and produces expected install data."""
    path = "/config/blueprints/automation/Blackshome/sensor-light.yaml"
    source_url = "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238"
    raw_content = SENSOR_LIGHT_PATTERN

    # 1. Simulate remote fetch: computes remote_hash and ensures source_url
    remote_hash = hash_content(raw_content, source_url)
    remote_content_with_url = ensure_source_url(raw_content, source_url)

    # 2. Simulate install preparation
    with patch("os.path.realpath", return_value=path):
        prepared = coordinator._prepare_blueprint_install(
            path,
            remote_content_with_url,
            remote_hash=remote_hash,
            source_url=source_url,
        )

    assert prepared.source_url == source_url
    assert prepared.real_path == path
    assert prepared.functional_domain == "automation"
    assert prepared.name == "Sensor Light"
    assert prepared.content.count("source_url:") == 1
    assert "reorder: false" not in prepared.content
    assert "multiple: false" not in prepared.content

    expected_hash = hash_content(prepared.content, source_url)
    assert remote_hash == expected_hash


def test_local_scan_matches_remote_hash_after_install() -> None:
    """Test that local scan of installed content produces the exact same hash as remote."""
    source_url = "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2"
    raw_remote = LOW_BATTERY_PATTERN

    # Content installed to disk
    installed_content = ensure_source_url(raw_remote, source_url)

    # Scanned locally from disk
    parsed_local = BlueprintUpdateCoordinator._parse_blueprint_data(
        "/config/blueprints/automation/test.yaml", installed_content
    )
    assert parsed_local is not None
    local_hash = parsed_local["local_hash"]

    # Remote hash computed from upstream raw content
    remote_hash = hash_content(raw_remote, source_url)

    assert local_hash == remote_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bp_name", "path", "raw_remote", "source_url"),
    [
        (
            "Sensor Light",
            "/config/blueprints/automation/Blackshome/sensor-light.yaml",
            SENSOR_LIGHT_PATTERN,
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238",
        ),
        (
            "Low Battery Notifications & Actions",
            "/config/blueprints/automation/Blackshome/low-battery.yaml",
            LOW_BATTERY_PATTERN,
            "https://gist.github.com/Blackshome/4010fb83bb8c19b5fa1425526c6ff0e2",
        ),
        (
            "Advanced Heating Control",
            "/config/blueprints/automation/panhans/advanced_heating_control.yaml",
            ADVANCED_HEATING_PATTERN,
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
        ),
        (
            "Target Area Select Blueprint",
            "/config/blueprints/automation/example/target_area.yaml",
            TARGET_AREA_SELECT_PATTERN,
            "https://github.com/example/blueprints/blob/main/target_area.yaml",
        ),
        (
            "Tuya Smart Knob",
            "/config/blueprints/automation/tuya/smart_knob.yaml",
            TUYA_SMART_KNOB_PATTERN,
            TUYA_SMART_KNOB_FORUM_URL,
        ),
    ],
)
async def test_coordinator_fetch_install_unit_argument_delegation(
    coordinator: BlueprintUpdateCoordinator,
    bp_name: str,
    path: str,
    raw_remote: str,
    source_url: str,
) -> None:
    """Test unit-level coordinator install argument construction and delegation."""
    # Phase 1: Local file exists before update check (imported by HA with injected source_url)
    imported_local = ensure_source_url(raw_remote, source_url)
    parsed = coordinator._parse_blueprint_data(path, imported_local)
    assert parsed is not None
    initial_local_hash = parsed["local_hash"]

    # Phase 2: Remote content fetched and processed
    remote_hash = hash_content(raw_remote, source_url)
    assert initial_local_hash == remote_hash

    # Phase 3: Install blueprint via public coordinator method
    remote_content_with_url = ensure_source_url(raw_remote, source_url)
    install_result = FileTransactionResult(
        content_hash=hashlib.sha256(remote_content_with_url.encode("utf-8")).hexdigest(),
        backups_count=0,
    )
    with (
        patch("os.path.realpath", return_value=path),
        patch.object(
            coordinator._file_store, "install", return_value=install_result
        ) as mock_install,
    ):
        await coordinator.async_install_blueprint(
            path,
            remote_content=remote_content_with_url,
            remote_hash=remote_hash,
            source_url=source_url,
            backup=False,
            reload_services=False,
        )

    # Invariant: installation succeeded and wrote the exact canonical content
    mock_install.assert_called_once()
    bound_args = inspect.signature(coordinator._file_store.install).bind(
        *mock_install.call_args.args, **mock_install.call_args.kwargs
    )
    bound_args.apply_defaults()
    assert bound_args.arguments["file_path"] == path
    installed_content = bound_args.arguments["content"]
    assert bound_args.arguments["max_backups"] == get_max_backups(coordinator.config_entry)
    assert bound_args.arguments["create_backup"] is False  # backup=False
    assert bound_args.arguments["precondition"] is None  # precondition=None
    assert isinstance(installed_content, str)
    assert installed_content.count("source_url:") == 1
    assert source_url in installed_content

    # Phase 4: Simulate Home Assistant restart / scan of the newly installed file
    restarted_parsed = coordinator._parse_blueprint_data(path, installed_content)
    assert restarted_parsed is not None
    restarted_local_hash = restarted_parsed["local_hash"]

    # Invariant: local hash after restart MUST match the remote hash from metadata
    assert restarted_local_hash == remote_hash

    # Phase 5: Coordinator startup merge with persisted remote_hash
    restarted_data: dict[str, dict[str, object]] = {
        path: {
            "name": bp_name,
            "domain": "automation",
            "source_url": source_url,
            "local_hash": restarted_local_hash,
            "remote_hash": remote_hash,
            "updatable": False,
        }
    }
    coordinator.data = {}
    coordinator._merge_previous_data(restarted_data)

    # Invariant: Blueprint must NOT be flagged as updatable (no ghost update)
    assert restarted_data[path]["updatable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bp_name", "rel_path", "raw_remote", "source_url"),
    [
        (
            "Sensor Light Blueprint",
            "automation/blackshome/sensor_light.yaml",
            SENSOR_LIGHT_PATTERN,
            "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238",
        ),
        (
            "Low Battery Blueprint",
            "automation/blackshome/low_battery.yaml",
            LOW_BATTERY_PATTERN,
            "https://gist.github.com/Blackshome/1234567890abcdef",
        ),
        (
            "Advanced Heating Blueprint",
            "automation/panhans/advanced_heating.yaml",
            ADVANCED_HEATING_PATTERN,
            "https://github.com/panhans/HomeAssistant/blob/main/blueprints/automation/panhans/advanced_heating_control.yaml",
        ),
        (
            "Tuya Smart Knob Blueprint",
            "automation/tuya/smart_knob.yaml",
            TUYA_SMART_KNOB_PATTERN,
            TUYA_SMART_KNOB_FORUM_URL,
        ),
    ],
)
async def test_coordinator_fetch_install_and_restart_integration_lifecycle(
    coordinator: BlueprintUpdateCoordinator,
    tmp_path: Path,
    bp_name: str,
    rel_path: str,
    raw_remote: str,
    source_url: str,
) -> None:
    """Test full filesystem-backed integration lifecycle (write to disk, re-scan, restart)."""
    target_path = str(tmp_path / rel_path)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    # 1. Initial import simulation (write initial file to disk with source_url)
    initial_content = ensure_source_url(raw_remote, source_url)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(initial_content)

    # 2. Parse from disk
    parsed = coordinator._parse_blueprint_data(target_path, initial_content)
    assert parsed is not None
    initial_local_hash = parsed["local_hash"]

    remote_hash = hash_content(raw_remote, source_url)
    assert initial_local_hash == remote_hash

    # 3. Perform real disk installation via coordinator (using real FileStore write)
    remote_content_with_url = ensure_source_url(raw_remote, source_url)
    with patch.object(coordinator, "_is_safe_path", return_value=True):
        await coordinator.async_install_blueprint(
            target_path,
            remote_content=remote_content_with_url,
            remote_hash=remote_hash,
            source_url=source_url,
            backup=True,
            reload_services=False,
        )

    # 4. Verify file was written to disk and can be read back
    assert os.path.exists(target_path)
    with open(target_path, encoding="utf-8") as f:
        persisted_content = f.read()

    assert source_url in persisted_content
    assert persisted_content.count("source_url:") == 1

    # 5. Simulate restart / re-scan of newly installed file from disk
    restarted_parsed = coordinator._parse_blueprint_data(target_path, persisted_content)
    assert restarted_parsed is not None
    assert restarted_parsed["local_hash"] == remote_hash

    # 6. Verify merge ensures updatable is False (no ghost update)
    restarted_data: dict[str, dict[str, object]] = {
        target_path: {
            "name": bp_name,
            "domain": "automation",
            "source_url": source_url,
            "local_hash": restarted_parsed["local_hash"],
            "remote_hash": remote_hash,
            "updatable": False,
        }
    }
    coordinator.data = {}
    coordinator._merge_previous_data(restarted_data)
    assert restarted_data[target_path]["updatable"] is False


@pytest.mark.asyncio
async def test_coordinator_install_failure_propagation(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that file store install errors propagate and leave coordinator state uncorrupted."""
    path = "/config/blueprints/automation/Blackshome/sensor-light.yaml"
    source_url = "https://gist.github.com/Blackshome/6edfec0ff6a25c5da0d07b88dc908238"
    raw_remote = SENSOR_LIGHT_PATTERN
    remote_hash = hash_content(raw_remote, source_url)
    remote_content = ensure_source_url(raw_remote, source_url)

    # 1. When path is not present in coordinator.data, failure does not add it
    coordinator.data = {}

    with (
        patch("os.path.realpath", return_value=path),
        patch.object(
            coordinator._file_store,
            "install",
            side_effect=HomeAssistantError("Disk write failed"),
        ),
        pytest.raises(HomeAssistantError, match="Disk write failed"),
    ):
        await coordinator.async_install_blueprint(
            path,
            remote_content=remote_content,
            remote_hash=remote_hash,
            source_url=source_url,
            backup=False,
            reload_services=False,
        )

    assert path not in coordinator.data

    # 2. When path is already present in coordinator.data, failure preserves existing state
    existing_entry: dict[str, object] = {
        "name": "Existing Sensor Light",
        "domain": "automation",
        "source_url": source_url,
        "local_hash": "existing_local_hash",
        "remote_hash": "existing_remote_hash",
        "updatable": True,
    }
    coordinator.data = {path: dict(existing_entry)}

    with (
        patch("os.path.realpath", return_value=path),
        patch.object(
            coordinator._file_store,
            "install",
            side_effect=HomeAssistantError("Disk write failed"),
        ),
        pytest.raises(HomeAssistantError, match="Disk write failed"),
    ):
        await coordinator.async_install_blueprint(
            path,
            remote_content=remote_content,
            remote_hash=remote_hash,
            source_url=source_url,
            backup=False,
            reload_services=False,
        )

    assert coordinator.data[path] == existing_entry


def test_multi_input_disambiguation_no_cross_wiping() -> None:
    """Verify that multiple inputs with the same selector type do not cause cross-wiping."""
    source_url = "https://github.com/user/test_multi_input"
    raw_content = """blueprint:
  name: Multi Input Test
  domain: automation
  input:
    input_empty:
      name: Empty Text
      selector:
        text: {}
    input_multiline:
      name: Multiline Text
      selector:
        text:
          multiline: true
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "multiline: true" in normalized
    assert "input_empty" in normalized
    # input_empty selector should be clean text: without braces
    assert "text: {}" not in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    assert inputs["input_empty"]["selector"]["text"] is None
    assert inputs["input_multiline"]["selector"]["text"] == {"multiline": True}


def test_clean_empty_selectors_no_braces() -> None:
    """Verify that empty selectors serialize cleanly without mapping braces."""
    source_url = "https://github.com/user/test_clean_empty"
    raw_content = """blueprint:
  name: Clean Selectors Test
  domain: automation
  input:
    bool_in:
      name: Boolean Input
      selector:
        boolean: {}
    text_in:
      name: Text Input
      selector:
        text: {}
    entity_in:
      name: Entity Input
      selector:
        entity: {}
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "{}" not in normalized
    assert "boolean:" in normalized
    assert "text:" in normalized
    assert "entity:" in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    assert inputs["bool_in"]["selector"]["boolean"] is None
    assert inputs["text_in"]["selector"]["text"] is None
    assert inputs["entity_in"]["selector"]["entity"] is None


def test_pruned_voluptuous_defaults() -> None:
    """Verify that synthetic voluptuous defaults are pruned from serialized output."""
    source_url = "https://github.com/user/test_prune_defaults"
    raw_content = """blueprint:
  name: Prune Defaults Test
  domain: automation
  input:
    num_in:
      name: Number Input
      selector:
        number:
          min: 1
          max: 100
    sel_in:
      name: Select Input
      selector:
        select:
          mode: dropdown
          options:
            - "Option A"
            - "Option B"
    txt_in:
      name: Text Input
      selector:
        text:
          multiline: true
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "step:" not in normalized
    assert "mode: slider" not in normalized
    assert "custom_value:" not in normalized
    assert "sort:" not in normalized
    assert "multiple:" not in normalized
    assert "min: 1" in normalized
    assert "max: 100" in normalized
    assert "mode: dropdown" in normalized
    assert "multiline: true" in normalized


def test_author_numeric_scalar_representation() -> None:
    """Verify that integer and float scalar representations are preserved."""
    source_url = "https://github.com/user/test_numeric_scalars"
    raw_content = """blueprint:
  name: Numeric Scalars Test
  domain: automation
  input:
    slider_input:
      name: Slider
      selector:
        number:
          min: 5
          max: 50
          step: 0.5
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "min: 5" in normalized
    assert "max: 50" in normalized
    assert "step: 0.5" in normalized
    assert "min: 5.0" not in normalized
    assert "max: 50.0" not in normalized

    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    num_selector = inputs["slider_input"]["selector"]["number"]
    assert isinstance(num_selector["min"], int)
    assert num_selector["min"] == 5
    assert isinstance(num_selector["max"], int)
    assert num_selector["max"] == 50
    assert isinstance(num_selector["step"], float)
    assert num_selector["step"] == 0.5


def test_metadata_ordering_invariance() -> None:
    """Verify that altering source_url line position produces identical SHA-256 hashes."""
    source_url = "https://github.com/user/test_ordering"
    content_url_top = f"""blueprint:
  source_url: {source_url}
  name: Ordering Test
  domain: automation
  author: Test Author
  description: Ordering test blueprint
"""
    content_url_bottom = f"""blueprint:
  name: Ordering Test
  author: Test Author
  description: Ordering test blueprint
  domain: automation
  source_url: {source_url}
"""
    content_no_url = """blueprint:
  name: Ordering Test
  author: Test Author
  description: Ordering test blueprint
  domain: automation
"""

    hash_top = hash_content(content_url_top, source_url)
    hash_bottom = hash_content(content_url_bottom, source_url)
    hash_injected = hash_content(content_no_url, source_url)

    assert hash_top == hash_bottom == hash_injected

    # Custom/future keys reordering invariance (e.g. version, icon)
    content_custom_ordered = f"""blueprint:
  name: Ordering Test
  domain: automation
  author: Test Author
  description: Ordering test blueprint
  icon: mdi:bell
  version: 2.1.0
  source_url: {source_url}
"""
    content_custom_scrambled = f"""blueprint:
  version: 2.1.0
  source_url: {source_url}
  icon: mdi:bell
  description: Ordering test blueprint
  author: Test Author
  domain: automation
  name: Ordering Test
"""
    norm_custom_1 = ensure_source_url(content_custom_ordered, source_url)
    norm_custom_2 = ensure_source_url(content_custom_scrambled, source_url)
    assert norm_custom_1 == norm_custom_2

    hash_custom_1 = hash_content(content_custom_ordered, source_url)
    hash_custom_2 = hash_content(content_custom_scrambled, source_url)
    assert hash_custom_1 == hash_custom_2


def test_fallback_resilience_description_input() -> None:
    """Verify that blueprints with description: !input fallback safely without exception."""
    source_url = "https://github.com/user/test_fallback"
    raw_content = """blueprint:
  name: Fallback Test
  domain: automation
  input:
    prompt:
      name: Prompt
      selector:
        text: {}
      description: !input prompt_var
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert source_url in normalized
    assert "Fallback Test" in normalized
    assert "{}" not in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    assert inputs["prompt"]["description"] == Input("prompt_var")
    assert inputs["prompt"]["selector"]["text"] is None


def test_fallback_resilience_schema_exception_forced() -> None:
    """Verify fallback cleans empty selectors and preserves metadata on failure."""
    source_url = "https://github.com/user/test_forced_fallback"
    raw_content = """blueprint:
  name: Forced Fallback Test
  domain: automation
  custom_tag: author_metadata
  extra_info:
    category: lighting
    priority: 1
  input:
    forced_in:
      name: Forced Input
      selector:
        boolean: {}
"""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation.get_blueprint_schema",
        side_effect=vol.Invalid("forced validation error"),
    ):
        normalized = ensure_source_url(raw_content, source_url)

    assert source_url in normalized
    assert "{}" not in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    assert bp_data["name"] == "Forced Fallback Test"
    assert bp_data["domain"] == "automation"
    assert bp_data["custom_tag"] == "author_metadata"
    assert bp_data["extra_info"] == {"category": "lighting", "priority": 1}
    assert bp_data["source_url"] == source_url
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    assert inputs["forced_in"]["selector"]["boolean"] is None


def test_normalization_idempotency_multi_pass() -> None:
    """Verify that multi-pass normalization is fully idempotent across multiple passes."""
    source_url = "https://github.com/user/test_idempotency"
    raw_content = """blueprint:
  name: Idempotency Test
  domain: automation
  input:
    my_num:
      name: Number
      selector:
        number:
          max: 100
          min: 1
    my_text:
      name: Text
      selector:
        text: {}
"""
    pass1 = ensure_source_url(raw_content, source_url)
    pass2 = ensure_source_url(pass1, source_url)
    pass3 = ensure_source_url(pass2, source_url)

    assert pass1 == pass2 == pass3
    hash1 = hash_content(pass1, source_url)
    hash2 = hash_content(pass2, source_url)
    hash3 = hash_content(pass3, source_url)
    assert hash1 == hash2 == hash3


def test_get_blueprint_canonical_keys_discovery() -> None:
    """Verify get_blueprint_canonical_keys discovers canonical keys and excludes source_url."""
    keys = get_blueprint_canonical_keys()
    assert isinstance(keys, tuple)
    assert len(keys) >= 4
    assert "name" in keys
    assert "domain" in keys
    assert "input" in keys
    assert "source_url" not in keys


def test_order_blueprint_metadata_future_keys_pinned_before_source_url() -> None:
    """Verify that future or custom metadata keys are sorted deterministically before source_url."""
    raw_bp: dict[object, object] = {
        "source_url": "https://github.com/user/test",
        "version": "2.1.0",
        "name": "Future Blueprint",
        "icon": "mdi:rocket",
        "author": "Alice",
        "domain": "automation",
        "input": {"btn": {"name": "Button"}},
    }
    ordered = _order_blueprint_metadata(raw_bp)
    ordered_keys = list(ordered.keys())

    # source_url must always be the final element at the tail
    assert ordered_keys[-1] == "source_url"

    # Future/custom keys (icon, version) must precede source_url
    icon_idx = ordered_keys.index("icon")
    version_idx = ordered_keys.index("version")
    source_url_idx = ordered_keys.index("source_url")
    assert icon_idx < source_url_idx
    assert version_idx < source_url_idx
    # Custom keys must be sorted alphabetically
    assert icon_idx < version_idx


def test_order_blueprint_metadata_without_source_url() -> None:
    """Verify that metadata without source_url does not have source_url injected."""
    raw_bp: dict[object, object] = {
        "domain": "automation",
        "name": "No Source URL",
        "custom_tag": "blueprints",
    }
    ordered = _order_blueprint_metadata(raw_bp)
    assert "source_url" not in ordered
    assert list(ordered.keys()) == ["name", "domain", "custom_tag"]


def test_order_blueprint_metadata_non_string_keys() -> None:
    """Verify non-string metadata keys are deterministically ordered with tie-breaking."""
    raw_bp: dict[object, object] = {
        123: "numeric_key",
        "source_url": "https://example.com/bp",
        "domain": "automation",
        "name": "Non-String Keys",
        "custom": "val",
    }
    ordered = _order_blueprint_metadata(raw_bp)
    assert "source_url" in ordered
    assert list(ordered.keys()) == ["name", "domain", 123, "custom", "source_url"]

    # Verify tie-breaker determinism for distinct keys with identical str() representations
    bp_int_first: dict[object, object] = {
        123: "int_key",
        "123": "str_key",
        "name": "Tie Breaker",
    }
    bp_str_first: dict[object, object] = {
        "123": "str_key",
        123: "int_key",
        "name": "Tie Breaker",
    }
    ordered_int_first = _order_blueprint_metadata(bp_int_first)
    ordered_str_first = _order_blueprint_metadata(bp_str_first)
    assert list(ordered_int_first.keys()) == ["name", 123, "123"]
    assert list(ordered_str_first.keys()) == ["name", 123, "123"]


def test_order_blueprint_metadata_static_canonical_ordering_independent_of_dynamic_schema() -> None:
    """Verify metadata ordering remains pinned to static keys regardless of dynamic schema order."""
    raw_bp: dict[object, object] = {
        "input": {},
        "name": "Test",
        "author": "Alice",
        "domain": "automation",
        "description": "Desc",
        "source_url": "https://example.com/bp",
    }
    with patch.object(
        BlueprintSchemaAdapter,
        "get_canonical_keys",
        return_value=("input", "author", "domain", "description", "name"),
    ):
        ordered = _order_blueprint_metadata(raw_bp)

    assert list(ordered.keys()) == [
        "name",
        "description",
        "domain",
        "author",
        "input",
        "source_url",
    ]


def test_ensure_source_url_non_string_metadata_key() -> None:
    """Verify non-string metadata keys serialize stably and produce invariant hashes."""
    source_url = "https://github.com/user/test_numeric_meta"
    raw_content = f"""blueprint:
  name: Non-String Key Test
  domain: automation
  123: numeric_metadata
  source_url: {source_url}
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "123: numeric_metadata" in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    assert bp_data[123] == "numeric_metadata"
    assert bp_data["source_url"] == source_url

    h1 = hash_content(raw_content, source_url)
    h2 = hash_content(normalized, source_url)
    assert h1 == h2


def test_ensure_source_url_future_keys_invariance() -> None:
    """Verify blueprints with future metadata keys produce invariant hashes."""
    source_url = "https://github.com/user/test_future_keys"
    content_top = f"""blueprint:
  source_url: {source_url}
  name: Future Keys Invariance
  domain: automation
  icon: mdi:home
  version: 1.0.0
  input:
    test_in:
      name: Test Input
      selector:
        text: {{}}
"""
    content_bottom = f"""blueprint:
  name: Future Keys Invariance
  domain: automation
  icon: mdi:home
  version: 1.0.0
  input:
    test_in:
      name: Test Input
      selector:
        text: {{}}
  source_url: {source_url}
"""
    content_no_url = """blueprint:
  name: Future Keys Invariance
  domain: automation
  icon: mdi:home
  version: 1.0.0
  input:
    test_in:
      name: Test Input
      selector:
        text: {}
"""
    hash_top = hash_content(content_top, source_url)
    hash_bottom = hash_content(content_bottom, source_url)
    hash_no_url = hash_content(content_no_url, source_url)

    assert hash_top == hash_bottom == hash_no_url
    normalized = ensure_source_url(content_top, source_url)
    # Ensure source_url is at the bottom after version and icon
    assert "icon: mdi:home" in normalized
    assert "version: 1.0.0" in normalized
    assert "{}" not in normalized


def test_get_blueprint_canonical_keys_mocked_schema() -> None:
    """Verify get_blueprint_canonical_keys orders custom schema keys and excludes source_url."""
    from unittest.mock import MagicMock

    from homeassistant.components.blueprint.const import CONF_BLUEPRINT, CONF_SOURCE_URL

    mock_schema = MagicMock()
    mock_inner = MagicMock()
    mock_inner.schema = {
        "custom_first": str,
        "name": str,
        CONF_SOURCE_URL: str,
        "input": dict,
        "domain": str,
    }
    mock_schema.schema = {CONF_BLUEPRINT: mock_inner}

    get_blueprint_canonical_keys.cache_clear()
    try:
        with patch(
            "custom_components.blueprints_updater.blueprint_validation.BLUEPRINT_SCHEMA",
            mock_schema,
        ):
            keys = get_blueprint_canonical_keys()
            assert keys == ("custom_first", "name", "input", "domain")
            assert CONF_SOURCE_URL not in keys
    finally:
        get_blueprint_canonical_keys.cache_clear()


def test_get_blueprint_canonical_keys_fallback_on_corrupt_schema() -> None:
    """Verify get_blueprint_canonical_keys falls back to static sequence on corrupt schema."""
    get_blueprint_canonical_keys.cache_clear()
    try:
        with patch(
            "custom_components.blueprints_updater.blueprint_validation.BLUEPRINT_SCHEMA",
            None,
        ):
            keys = get_blueprint_canonical_keys()
            assert keys == (
                "name",
                "description",
                "domain",
                "author",
                "homeassistant",
                "input",
            )
    finally:
        get_blueprint_canonical_keys.cache_clear()


def test_ensure_source_url_deepcopy_failure_fallback() -> None:
    """Verify ensure_source_url gracefully falls back to canonical YAML on deepcopy failure."""
    source_url = "https://github.com/user/test_deepcopy_failure"
    raw_content = """blueprint:
  name: Deepcopy Fallback Test
  domain: automation
  input:
    field:
      name: Field
      selector:
        text: {}
trigger:
  - platform: state
"""
    with (
        patch(
            "custom_components.blueprints_updater.blueprint_validation._safe_deepcopy",
            side_effect=copy.Error("mock deepcopy failure"),
        ),
        patch(
            "custom_components.blueprints_updater.blueprint_validation._LOGGER.debug"
        ) as mock_debug,
    ):
        normalized = ensure_source_url(raw_content, source_url)

    assert source_url in normalized
    assert "Deepcopy Fallback Test" in normalized
    assert "{}" not in normalized
    assert "trigger:" in normalized
    assert "triggers:" not in normalized
    assert any("falling back to canonical YAML" in str(c) for c in mock_debug.call_args_list)


def test_ensure_source_url_fallback_preserves_non_automation_keys() -> None:
    """Verify fallback does not rewrite root keys for non-automation blueprints."""
    source_url = "https://github.com/user/test_script_fallback"
    raw_content = """blueprint:
  name: Script Fallback Test
  domain: script
action:
  - custom_field: test
"""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation._safe_deepcopy",
        side_effect=copy.Error("mock deepcopy failure"),
    ):
        normalized = ensure_source_url(raw_content, source_url)

    assert source_url in normalized
    assert "action:" in normalized
    assert "actions:" not in normalized


def test_blueprint_schema_adapter_with_marker_keys() -> None:
    """Verify BlueprintSchemaAdapter correctly extracts keys from marker-wrapped schemas."""
    from unittest.mock import MagicMock

    from homeassistant.components.blueprint.const import CONF_BLUEPRINT, CONF_SOURCE_URL

    marker_bp = vol.Required(CONF_BLUEPRINT)
    marker_name = vol.Required("name")
    marker_domain = vol.Required("domain")
    marker_desc = vol.Optional("description")
    marker_source = vol.Optional(CONF_SOURCE_URL)
    marker_input = vol.Optional("input")

    mock_inner = MagicMock()
    mock_inner.schema = {
        marker_name: str,
        marker_desc: str,
        marker_domain: str,
        marker_source: str,
        marker_input: dict,
    }

    mock_schema = MagicMock()
    mock_schema.schema = {marker_bp: mock_inner}

    keys = BlueprintSchemaAdapter.get_canonical_keys(mock_schema)
    assert keys == ("name", "description", "domain", "input")
    assert CONF_SOURCE_URL not in keys


def test_blueprint_schema_adapter_preserves_input_declaration_order() -> None:
    """Verify BlueprintSchemaAdapter preserves schema order when input precedes other keys."""
    schema_with_early_input: dict[object, object] = {
        "input": dict,
        "name": str,
        "domain": str,
        "author": str,
    }
    keys = BlueprintSchemaAdapter.get_canonical_keys(schema_with_early_input)
    assert keys == ("input", "name", "domain", "author")


def test_blueprint_schema_adapter_direct_mapping() -> None:
    """Verify BlueprintSchemaAdapter recognizes directly supplied blueprint schema mappings."""
    direct_schema: dict[object, object] = {
        "name": str,
        "domain": str,
        "author": str,
        "description": str,
        "input": dict,
    }
    keys = BlueprintSchemaAdapter.get_canonical_keys(direct_schema)
    assert keys == ("name", "domain", "author", "description", "input")


def test_blueprint_schema_adapter_invalid_object_fallback() -> None:
    """Verify BlueprintSchemaAdapter falls back on corrupt schemas with logging."""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation._LOGGER.debug"
    ) as mock_debug:
        keys = BlueprintSchemaAdapter.get_canonical_keys(object())

    assert keys == (
        "name",
        "description",
        "domain",
        "author",
        "homeassistant",
        "input",
    )
    assert any(
        "falling back to default canonical keys" in str(c) for c in mock_debug.call_args_list
    )


def test_safe_deepcopy_unsupported_leaf() -> None:
    """Verify _safe_deepcopy isolates dictionaries while gracefully retaining uncopyable leaves."""

    class UncopyableLeaf:
        """Leaf type that raises copy.Error when deepcopied."""

        def __deepcopy__(self, memo: dict[int, object]) -> "UncopyableLeaf":
            """Simulate deepcopy failure on unsupported leaf object.

            Args:
                memo: Memoization dictionary.

            Returns:
                Never returns; raises copy.Error.

            """
            raise copy.Error("leaf cannot be deepcopied")

    leaf = UncopyableLeaf()
    data: dict[str, object] = {
        "nested": {"key": "val", "leaf": leaf},
        "items": [1, 2, leaf],
    }

    copied = _safe_deepcopy(data)
    assert isinstance(copied, dict)
    assert copied is not data
    nested_copied = copied["nested"]
    assert isinstance(nested_copied, dict)
    assert nested_copied is not data["nested"]
    assert nested_copied["leaf"] is leaf
    assert copied["items"] is not data["items"]
    assert copied["items"][2] is leaf


def test_safe_deepcopy_arbitrary_leaf_exception() -> None:
    """Verify _safe_deepcopy handles arbitrary exceptions from custom leaf __deepcopy__."""

    class FailingLeaf:
        """Leaf type that raises arbitrary exception when deepcopied."""

        def __deepcopy__(self, memo: dict[int, object]) -> "FailingLeaf":
            """Simulate unexpected runtime failure on unsupported leaf object.

            Args:
                memo: Memoization dictionary.

            Returns:
                Never returns; raises RuntimeError.

            """
            raise RuntimeError("unexpected deepcopy failure in custom leaf")

    leaf = FailingLeaf()
    data: dict[str, object] = {"custom": leaf, "nested": {"leaf": leaf}}
    copied = _safe_deepcopy(data)

    assert isinstance(copied, dict)
    assert copied is not data
    assert copied["custom"] is leaf
    assert isinstance(copied["nested"], dict)
    assert copied["nested"]["leaf"] is leaf


def test_ensure_source_url_keeps_parsed_data_pristine() -> None:
    """Verify ensure_source_url does not mutate parsed structure during validation or fallback."""
    from homeassistant.util import yaml as yaml_util

    source_url = "https://github.com/user/test_pristine_parsed"
    raw_content = """blueprint:
  name: Pristine Parsed Test
  domain: automation
  custom_tag: original_value
trigger:
  - platform: state
"""
    parsed_before = yaml_util.parse_yaml(raw_content)
    assert isinstance(parsed_before, dict)

    with (
        patch(
            "custom_components.blueprints_updater.blueprint_validation._safe_deepcopy",
            side_effect=copy.Error("simulate deepcopy crash"),
        ),
        patch(
            "homeassistant.util.yaml.parse_yaml",
            return_value=parsed_before,
        ),
    ):
        normalized = ensure_source_url(raw_content, source_url)

    assert source_url in normalized
    assert "Pristine Parsed Test" in normalized
    # Verify parsed blueprint dict was not modified with source_url
    assert "source_url" not in parsed_before["blueprint"]


def test_ensure_source_url_unexpected_exception_surfaces() -> None:
    """Verify ensure_source_url does not swallow unexpected runtime/programming errors."""
    source_url = "https://github.com/user/test_unexpected_exception"
    raw_content = """blueprint:
  name: Unexpected Error Test
  domain: automation
"""
    with (
        patch(
            "custom_components.blueprints_updater.blueprint_validation.get_blueprint_schema",
            side_effect=RuntimeError("unexpected programming bug"),
        ),
        pytest.raises(RuntimeError, match="unexpected programming bug"),
    ):
        ensure_source_url(raw_content, source_url)


def test_stabilize_dict_structure_empty_orig_dict_selector_level2() -> None:
    """Verify Level 2 selector stabilization prunes defaults when orig_dict is empty."""
    orig_empty: dict[object, object] = {}
    norm_defaults: dict[object, object] = {
        "min": 0,
        "max": 100,
        "step": 1.0,
        "mode": "slider",
    }
    stabilized = _stabilize_dict_structure(
        orig_empty,
        norm_defaults,
        selector_path=("number",),
        filter_paths=frozenset(),
    )
    assert stabilized == {}


def test_stabilize_dict_structure_unmatched_key_passes_none() -> None:
    """Verify unmatched norm keys pass None as original value to stabilize_yaml_structure."""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation.stabilize_yaml_structure",
        wraps=stabilize_yaml_structure,
    ) as mock_stabilize:
        _stabilize_dict_structure(
            {"matched": "val"},
            {"matched": "val", "unmatched": "default"},
            selector_path=None,
            filter_paths=frozenset(),
        )
        unmatched_calls = [c for c in mock_stabilize.call_args_list if c[0][1] == "default"]
        assert len(unmatched_calls) == 1
        assert unmatched_calls[0][0][0] is None


def test_clean_empty_selectors_post_processing_success_path() -> None:
    """Verify _clean_empty_selectors cleans empty selector mapping in successful normalization."""
    source_url = "https://github.com/user/test_clean_empty"
    raw_content = """blueprint:
  name: Clean Success Test
  domain: automation
  input:
    txt:
      name: Text Input
      selector:
        text: {}
"""
    normalized = ensure_source_url(raw_content, source_url)
    assert "text: {}" not in normalized
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    bp_data = parsed.get("blueprint")
    assert isinstance(bp_data, dict)
    inputs = bp_data.get("input")
    assert isinstance(inputs, dict)
    assert inputs["txt"]["selector"]["text"] is None


def test_clean_empty_selectors_does_not_mutate_service_payloads() -> None:
    """Verify empty-selector cleanup is restricted to blueprint inputs.

    Service payloads containing selector mappings with empty values must be left intact.
    """
    source_url = "https://github.com/user/test_service_selector"
    raw_content = """blueprint:
  name: Service Payload Test
  domain: automation
  input:
    my_in:
      name: Input
      selector:
        text: {}
action:
  - action: test.service
    data:
      selector:
        text: {}
"""
    normalized = ensure_source_url(raw_content, source_url)
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    # Input selector is cleaned to None
    assert parsed["blueprint"]["input"]["my_in"]["selector"]["text"] is None
    # Action payload selector with empty dict is preserved as empty dict, not None
    action_list = parsed.get("actions") or parsed.get("action")
    assert isinstance(action_list, list)
    assert action_list[0]["data"]["selector"]["text"] == {}


def test_ensure_source_url_fallback_preserves_singular_spelling() -> None:
    """Verify fallback preserves singular key spellings and does not canonicalize to plural."""
    source_url = "https://github.com/user/test_fallback_singular"
    raw_content = """blueprint:
  name: Singular Fallback Test
  domain: automation
trigger:
  - platform: state
"""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation._safe_deepcopy",
        side_effect=copy.Error("mock deepcopy failure"),
    ):
        normalized = ensure_source_url(raw_content, source_url)

    assert source_url in normalized
    assert "trigger:" in normalized
    assert "triggers:" not in normalized


def test_ensure_source_url_fallback_preserves_colliding_aliases() -> None:
    """Verify fallback preserves both values when conflicting singular and plural aliases exist."""
    source_url = "https://github.com/user/test_fallback_colliding"
    raw_content = """blueprint:
  name: Colliding Fallback Test
  domain: automation
trigger:
  - platform: state
    entity_id: sensor.singular
triggers:
  - platform: state
    entity_id: sensor.plural
"""
    with patch(
        "custom_components.blueprints_updater.blueprint_validation._safe_deepcopy",
        side_effect=copy.Error("mock deepcopy failure"),
    ):
        normalized = ensure_source_url(raw_content, source_url)

    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    assert "trigger" in parsed
    assert "triggers" in parsed
    assert parsed["trigger"][0]["entity_id"] == "sensor.singular"
    assert parsed["triggers"][0]["entity_id"] == "sensor.plural"


def test_conflicting_aliases_rejected_and_handled_by_fallback() -> None:
    """Verify conflicting aliases are rejected before normalization.

    Both values are safely preserved via the fallback path.
    """
    source_url = "https://github.com/user/test_conflicting_aliases"
    raw_content = """blueprint:
  name: Conflicting Aliases Test
  domain: automation
trigger:
  - platform: state
    entity_id: sensor.sing
triggers:
  - platform: state
    entity_id: sensor.plur
"""
    normalized = ensure_source_url(raw_content, source_url)
    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    assert "trigger" in parsed
    assert "triggers" in parsed
    assert parsed["trigger"][0]["entity_id"] == "sensor.sing"
    assert parsed["triggers"][0]["entity_id"] == "sensor.plur"


def test_bidirectional_alias_matching_without_duplicate_serialization() -> None:
    """Verify schema normalizing plural to singular alias preserves orig_val.

    The duplicate alias key must not be serialized.
    """
    source_url = "https://github.com/user/test_bidirectional_alias"
    raw_content = """blueprint:
  name: Bidirectional Alias Test
  domain: automation
triggers:
  - platform: state
    entity_id: sensor.test
"""
    mock_schema = MagicMock()
    # Schema normalizes plural triggers to singular trigger
    mock_schema.return_value = {
        "blueprint": {
            "name": "Bidirectional Alias Test",
            "domain": "automation",
        },
        "trigger": [{"platform": "state", "entity_id": "sensor.test"}],
    }

    with patch(
        "custom_components.blueprints_updater.blueprint_validation.get_blueprint_schema",
        return_value=mock_schema,
    ):
        normalized = ensure_source_url(raw_content, source_url)

    parsed = yaml_util.parse_yaml(normalized)
    assert isinstance(parsed, dict)
    # The normalized singular key is present with the original value
    assert "trigger" in parsed
    assert parsed["trigger"] == [{"platform": "state", "entity_id": "sensor.test"}]
    # The duplicate input key "triggers" is NOT appended
    assert "triggers" not in parsed
