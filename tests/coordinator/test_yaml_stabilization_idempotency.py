"""Tests for YAML stabilization idempotency, ghost update elimination, and list scoping."""

import hashlib
import inspect
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.blueprint_validation import (
    canonicalize_source_url,
    ensure_source_url,
    hash_content,
    normalize_content,
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
        pass1 = ensure_source_url(raw_yaml, canonical_source_url)
        pass2 = ensure_source_url(pass1, canonical_source_url)
        pass3 = ensure_source_url(pass2, canonical_source_url)
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

    injected_1 = ensure_source_url(raw_content, canonical_url)
    injected_2 = ensure_source_url(injected_1, canonical_url)
    assert injected_1 == injected_2
    assert "source_url:" in injected_1

    # Injected content hash matches hash_content with source_url
    assert hash_content(injected_1) == hash_content(raw_content, source_url=url)


def test_tuya_smart_knob_forum_pattern_stabilization() -> None:
    """Test YAML stabilization and semantic hashing for Tuya Smart Knob HA forum blueprint."""
    source_url = TUYA_SMART_KNOB_FORUM_URL
    canonical_url = canonicalize_source_url(source_url)
    assert canonical_url == "https://community.home-assistant.io/t/787779"

    stabilized_pass1 = ensure_source_url(TUYA_SMART_KNOB_PATTERN, canonical_url)
    stabilized_pass2 = ensure_source_url(stabilized_pass1, canonical_url)
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
    assert prepared.content == remote_content_with_url

    expected_hash = hash_content(prepared.content, source_url)
    assert remote_hash == expected_hash


@pytest.mark.asyncio
async def test_prepare_blueprint_install_without_source_url_normalizes_content(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that _prepare_blueprint_install applies normalize_content when source_url is absent."""
    path = "/config/blueprints/automation/test/no-source-url.yaml"
    # Content with UTF-8 BOM and CRLF line endings
    raw_content = "\ufeffblueprint:\r\n  name: Test BOM\r\n  domain: automation\r\n"
    valid_hash = hash_content(raw_content, None)

    for empty_url in (None, "   "):
        with patch("os.path.realpath", return_value=path):
            prepared = coordinator._prepare_blueprint_install(
                path,
                raw_content,
                remote_hash=valid_hash,
                source_url=empty_url,
            )

        assert prepared.source_url is None
        assert "\ufeff" not in prepared.content
        assert "\r" not in prepared.content
        assert prepared.content == normalize_content(raw_content)
        assert hash_content(prepared.content, None) == valid_hash

    # Mismatched hash raises HomeAssistantError
    with (
        patch("os.path.realpath", return_value=path),
        pytest.raises(
            HomeAssistantError, match="Remote hash does not match validated install content"
        ),
    ):
        coordinator._prepare_blueprint_install(
            path,
            raw_content,
            remote_hash="invalid_hash_abc",
            source_url=None,
        )


@pytest.mark.asyncio
async def test_prepare_blueprint_install_flow_mapping_formats_canonical_yaml(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that _prepare_blueprint_install formats flow mapping blueprint as canonical YAML."""
    path = "/config/blueprints/automation/test/flow-mapping.yaml"
    target_url = "https://example.com/flow-blueprint.yaml"
    flow_content = "blueprint: {name: Flow Mapping, domain: automation}\n"
    expected_hash = hash_content(flow_content, target_url)

    with patch("os.path.realpath", return_value=path):
        prepared = coordinator._prepare_blueprint_install(
            path,
            flow_content,
            remote_hash=expected_hash,
            source_url=target_url,
        )

    assert prepared.source_url == target_url
    assert "source_url:" in prepared.content
    parsed = yaml_util.parse_yaml(prepared.content)
    assert isinstance(parsed, dict)
    assert parsed.get("blueprint", {}).get("source_url") == target_url
    assert hash_content(prepared.content, target_url) == expected_hash

    # Verify that a mismatched remote_hash is rejected
    with (
        patch("os.path.realpath", return_value=path),
        pytest.raises(
            HomeAssistantError, match="Remote hash does not match validated install content"
        ),
    ):
        coordinator._prepare_blueprint_install(
            path,
            flow_content,
            remote_hash="mismatched_remote_hash_12345",
            source_url=target_url,
        )


@pytest.mark.asyncio
async def test_prepare_blueprint_install_formats_canonical_yaml_via_ha_core(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that _prepare_blueprint_install produces canonical YAML aligned with HA Core."""
    path = "/config/blueprints/automation/test/preservation.yaml"
    old_url = "https://example.com/old_source.yaml"
    new_url = "https://example.com/new_source.yaml"
    raw_content = (
        "\ufeff# Top author comment\r\n"
        "blueprint:\r\n"
        "  name: Preserved Blueprint\r\n"
        "  author: luuquangvu\r\n"
        "  description: |-\r\n"
        "    Multiline scalar\r\n"
        "    preserved perfectly\r\n"
        f"  source_url: {old_url}\r\n"
        "  domain: automation\r\n"
        "  # Mid-section comment\r\n"
        "  input:\r\n"
        "    sensor:\r\n"
        "      name: Sensor\r\n"
    )
    expected_content = ensure_source_url(raw_content, new_url)
    expected_hash = hash_content(expected_content, new_url)

    with patch("os.path.realpath", return_value=path):
        prepared = coordinator._prepare_blueprint_install(
            path,
            raw_content,
            remote_hash=expected_hash,
            source_url=new_url,
        )

    assert prepared.source_url == new_url
    assert prepared.content == expected_content
    assert f"source_url: {new_url}" in prepared.content
    assert "# Top author comment" not in prepared.content
    assert "# Mid-section comment" not in prepared.content
    assert "\ufeff" not in prepared.content
    assert "\r\n" not in prepared.content
    parsed = yaml_util.parse_yaml(prepared.content)
    assert isinstance(parsed, dict)
    assert parsed["blueprint"]["source_url"] == new_url
    assert parsed["blueprint"]["name"] == "Preserved Blueprint"
    assert parsed["blueprint"]["author"] == "luuquangvu"
    assert "Multiline scalar\npreserved perfectly" in parsed["blueprint"]["description"]
    assert parsed["blueprint"]["domain"] == "automation"
    assert "sensor" in parsed["blueprint"]["input"]
    assert hash_content(prepared.content, new_url) == expected_hash

    # Verify rejection on mismatched remote hash
    with (
        patch("os.path.realpath", return_value=path),
        pytest.raises(
            HomeAssistantError, match="Remote hash does not match validated install content"
        ),
    ):
        coordinator._prepare_blueprint_install(
            path,
            raw_content,
            remote_hash="mismatched_preserved_hash_xyz",
            source_url=new_url,
        )


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
    assert inputs["input_empty"]["selector"]["text"] == {"multiline": False, "multiple": False}
    assert inputs["input_multiline"]["selector"]["text"] == {"multiline": True, "multiple": False}


def test_fallback_resilience_description_input() -> None:
    """Verify that blueprints with description: !input raise InvalidBlueprint without fallback."""
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
    with pytest.raises(InvalidBlueprint):
        ensure_source_url(raw_content, source_url)


def test_fallback_resilience_schema_exception_forced() -> None:
    """Verify schema exception raises InvalidBlueprint without fallback."""
    source_url = "https://github.com/user/test_forced_fallback"
    raw_content = """blueprint:
  domain: automation
"""
    with pytest.raises(InvalidBlueprint):
        ensure_source_url(raw_content, source_url)


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


def test_ensure_source_url_non_string_metadata_key() -> None:
    """Verify non-string metadata keys raise InvalidBlueprint under HA Core schema."""
    source_url = "https://github.com/user/test_numeric_meta"
    raw_content = f"""blueprint:
  name: Non-String Key Test
  domain: automation
  123: numeric_metadata
  source_url: {source_url}
"""
    with pytest.raises(InvalidBlueprint):
        ensure_source_url(raw_content, source_url)


def test_ensure_source_url_unexpected_exception_surfaces() -> None:
    """Verify ensure_source_url raises HomeAssistantError on unexpected runtime errors."""
    source_url = "https://github.com/user/test_unexpected_exception"
    raw_content = """blueprint:
  name: Unexpected Error Test
  domain: automation
"""
    with (
        patch(
            "homeassistant.components.blueprint.models.Blueprint.yaml",
            side_effect=RuntimeError("unexpected programming bug"),
        ),
        pytest.raises(
            HomeAssistantError,
            match="Failed to serialize blueprint YAML: unexpected programming bug",
        ),
    ):
        ensure_source_url(raw_content, source_url)
