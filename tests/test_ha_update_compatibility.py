"""Unit and integration tests for Home Assistant update compatibility guard."""

import asyncio
import os
import tempfile
from contextlib import nullcontext
from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.device_automation.exceptions import (
    EntityNotFound,
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import CONF_VARIABLES, __version__
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers import frame
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import yaml as yaml_util
from homeassistant.util.yaml.objects import Input

from custom_components.blueprints_updater import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.blueprints_updater.blueprint_validation import (
    _DEFAULT_MATH_FILTER_METHODS,
    _DEFAULT_MATH_GLOBALS,
    SyntheticDummyValues,
    _derive_value_for_path,
    _discover_ha_math_capabilities,
    _resolve_config_path,
    _wrap_action_target_blocks,
    detect_unsupported_yaml_constructs,
    extract_synthetic_dummy_values,
    is_dummy_validation_error,
    is_synthetic_identifier,
    modernize_legacy_blueprint_yaml,
)
from custom_components.blueprints_updater.const import (
    CONF_VERIFY_ON_HA_UPDATE,
    DOMAIN,
    STORAGE_KEY_LAST_HA_VERSION,
    FunctionalDomain,
    IncompatibilitySeverity,
    IntegrationService,
    PinReason,
    RepairForkAction,
    RepairIncompatibleAction,
    RepairIssueType,
)
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
    CompatibilityReport,
    ValidationDiagnostics,
    capture_structural_validation_diagnostics,
    diff_structural_configs,
)
from custom_components.blueprints_updater.file_store import (
    BlueprintFileStore,
    FileRevisionMismatchError,
    FileRevisionPrecondition,
)
from custom_components.blueprints_updater.repairs import (
    IncompatibleBlueprintRepairFlow,
    async_create_fix_flow,
)
from custom_components.blueprints_updater.utils import (
    get_ha_version,
    stringify_keys,
)


@pytest.fixture
def hass(_mock_hass):
    """Fixture providing mocked Home Assistant instance."""
    _mock_hass.config.version = "2024.12.0"
    return _mock_hass


@pytest.fixture
def coordinator(hass, monkeypatch) -> BlueprintUpdateCoordinator:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})
    entry.data = {}
    entry.entry_id = "test_entry_id"
    coord = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=24),
    )

    def _mock_set_data(data: dict) -> None:
        coord.data = data

    monkeypatch.setattr(coord, "async_set_updated_data", MagicMock(side_effect=_mock_set_data))
    monkeypatch.setattr(coord, "async_update_listeners", MagicMock())
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    monkeypatch.setattr(coord, "async_reconcile_reload_services", AsyncMock())
    coord.setup_complete = True
    coord.last_update_success = True
    monkeypatch.setattr(coord, "_filter_existing_metadata", lambda root, meta: meta)
    monkeypatch.setattr(coord, "_is_safe_path", MagicMock(return_value=True))
    monkeypatch.setattr(coord, "_is_safe_url", AsyncMock(return_value=True))
    hass.data = {DOMAIN: {"coordinators": {entry.entry_id: coord}}}
    return coord


async def test_version_comparison_and_state_storage(coordinator, hass):
    """Test Home Assistant version tracking and update detection."""
    # First boot: last_ha_version is None
    coordinator._last_ha_version = None
    assert await coordinator.async_check_ha_version_update() is False
    assert coordinator._last_ha_version == "2024.12.0"

    # Same version: no update
    assert await coordinator.async_check_ha_version_update() is False

    # Force: returns True
    assert await coordinator.async_check_ha_version_update(force=True) is True

    # Version upgraded
    hass.config.version = "2025.1.0"
    assert await coordinator.async_check_ha_version_update() is True

    # Save version
    await coordinator.async_save_ha_version("2025.1.0")
    assert coordinator._last_ha_version == "2025.1.0"
    assert await coordinator.async_check_ha_version_update() is False

    # Assert persisted storage payload and metadata preservation
    coordinator._store.async_save.assert_awaited()
    save_payload = coordinator._store.async_save.call_args[0][0]
    assert save_payload[STORAGE_KEY_LAST_HA_VERSION] == "2025.1.0"

    coordinator._persisted_metadata = {
        "automation/test.yaml": {"remote_hash": "abc1234", "pinned": True}
    }
    await coordinator.async_save_ha_version("2025.2.0")
    save_payload_with_meta = coordinator._store.async_save.call_args[0][0]
    assert save_payload_with_meta[STORAGE_KEY_LAST_HA_VERSION] == "2025.2.0"
    assert "automation/test.yaml" in save_payload_with_meta["metadata"]
    assert save_payload_with_meta["metadata"]["automation/test.yaml"]["remote_hash"] == "abc1234"
    assert save_payload_with_meta["metadata"]["automation/test.yaml"]["pinned"] is True

    # Verify failure resilience when async_save raises
    coordinator._store.async_save.side_effect = OSError("Disk write failed")
    prev_persisted_ver = coordinator._persisted_last_ha_version
    await coordinator.async_save_ha_version("2025.3.0")
    # In-memory version is updated but persisted version is NOT updated due to save failure
    assert coordinator._last_ha_version == "2025.3.0"
    assert coordinator._persisted_last_ha_version == prev_persisted_ver

    # Verify recovery once storage succeeds again
    coordinator._store.async_save.side_effect = None
    await coordinator.async_save_ha_version("2025.3.0")
    recovered_payload = coordinator._store.async_save.call_args[0][0]
    assert recovered_payload[STORAGE_KEY_LAST_HA_VERSION] == "2025.3.0"
    assert coordinator._persisted_last_ha_version == "2025.3.0"


async def test_local_blueprint_discovery(coordinator, hass):
    """Test discovering local blueprints with and without source_url across domains."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        hass.config.path.side_effect = lambda *args: os.path.join(tmp_dir, *args[1:])

        # Setup automation blueprint with source_url
        auto_dir = os.path.join(tmp_dir, "automation", "test_author")
        os.makedirs(auto_dir, exist_ok=True)
        auto_file = os.path.join(auto_dir, "auto.yaml")
        with open(auto_file, "w", encoding="utf-8") as f:
            f.write(
                "blueprint:\n"
                "  name: Auto Blueprint\n"
                "  domain: automation\n"
                "  source_url: https://github.com/author/repo/blob/main/auto.yaml\n"
            )

        # Setup local custom script blueprint without source_url
        script_dir = os.path.join(tmp_dir, "script", "local_custom")
        os.makedirs(script_dir, exist_ok=True)
        script_file = os.path.join(script_dir, "custom_script.yaml")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write("blueprint:\n  name: Custom Script\n  domain: script\n")

        # Discover blueprints
        blueprints = await coordinator.async_scan_all_local_blueprint_files()

        assert auto_file in blueprints
        assert blueprints[auto_file]["name"] == "Auto Blueprint"
        assert (
            blueprints[auto_file]["source_url"]
            == "https://github.com/author/repo/blob/main/auto.yaml"
        )
        assert blueprints[auto_file]["domain"] == FunctionalDomain.AUTOMATION

        assert script_file in blueprints
        assert blueprints[script_file]["name"] == "Custom Script"
        assert blueprints[script_file]["source_url"] == ""
        assert blueprints[script_file]["domain"] == FunctionalDomain.SCRIPT


def _assert_modernization_substitutions(
    modernized: str,
    *,
    expected_absent: tuple[str, ...],
    expected_present: tuple[str, ...],
) -> None:
    """Assert expected modernization substitutions are present and obsolete syntax is absent.

    Args:
        modernized: The modernized YAML string.
        expected_absent: Strings that must not appear in the modernized output.
        expected_present: Strings that must appear in the modernized output.

    """
    for item in expected_absent:
        assert item not in modernized
    for item in expected_present:
        assert item in modernized


def test_legacy_blueprint_modernization_engine() -> None:
    """Test modernization engine transformations."""
    legacy_content = (
        "blueprint:\n"
        "  name: Legacy Blueprint\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: sensor.temp\n"
        "  - platform: custom_integration_trigger\n"
        "    event_data: test\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    entity_id: light.living_room\n"
        "    data_template:\n"
        "      brightness: \"{{ math.floor(states('sensor.brightness') | float * 2.55) }}\"\n"
        "  - service_template: light.turn_off\n"
        "    entity_id: light.kitchen\n"
    )

    modernized = modernize_legacy_blueprint_yaml(
        legacy_content,
        FunctionalDomain.AUTOMATION,
        dynamic_replacements={"legacy_param": "modern_param"},
    )

    _assert_modernization_substitutions(
        modernized,
        expected_absent=(
            "service:",
            "service_template:",
            "data_template:",
            "math.floor",
            "floor(",
        ),
        expected_present=(
            "action: light.turn_on",
            "action: light.turn_off",
            "data:",
            "trigger: state",
            "round(0, 'floor')",
            "| float",
            "target:",
            "entity_id: light.living_room",
        ),
    )


def test_legacy_blueprint_modernization_list_target() -> None:
    """Test list of entity IDs target wrapping during blueprint modernization."""
    list_target_content = (
        "blueprint:\n"
        "  name: List Target BP\n"
        "  domain: automation\n"
        "  input:\n"
        "    service:\n"
        "      name: Service\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    entity_id:\n"
        "      - light.living_room\n"
        "      - light.kitchen\n"
        "    data:\n"
        '      brightness: "{{ math.floor(math.ceil(1.5)) }}"\n'
        "  - action: choose\n"
        "    choose:\n"
        "      - conditions:\n"
        "          - condition: state\n"
        "            entity_id: light.test\n"
        "            state: 'on'\n"
        "        sequence:\n"
        "          - delay: 5\n"
    )
    modernized_list = modernize_legacy_blueprint_yaml(
        list_target_content, FunctionalDomain.AUTOMATION
    )
    # Verify input service: is NOT renamed inside blueprint section
    assert "    service:\n" in modernized_list
    # Verify math is nested correctly
    assert "((1.5) | round(0, 'ceil')) | round(0, 'floor')" in modernized_list
    # Verify list target was wrapped under target: without breaking syntax
    assert "target:\n" in modernized_list
    assert "- light.living_room" in modernized_list
    assert "- light.kitchen" in modernized_list
    # Verify conditions inside choose are not wrapped
    assert "condition: state\n            entity_id: light.test" in modernized_list
    # Verify valid YAML parsing
    parsed = yaml_util.parse_yaml(modernized_list)
    assert isinstance(parsed, dict)
    action_item = parsed["action"][0]
    assert "target" in action_item
    assert action_item["target"]["entity_id"] == ["light.living_room", "light.kitchen"]


async def test_async_generate_modernized_candidate(coordinator, monkeypatch):
    """Test candidate generation and validation gating."""
    rel_path = "automation/test.yaml"
    full_path = "/config/blueprints/automation/test.yaml"
    content = (
        "blueprint:\n  name: Test\n  domain: automation\naction:\n  - service: light.turn_on\n"
    )

    # Case 1: Valid modernized candidate passes validation
    report_mock = CompatibilityReport(severity=None)
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=report_mock),
    )

    result = await coordinator.async_generate_modernized_candidate(
        rel_path, full_path, content, FunctionalDomain.AUTOMATION
    )
    assert result is not None
    modernized_content, diff_text = result
    assert "action: light.turn_on" in modernized_content
    assert "-  - service: light.turn_on" in diff_text
    assert "+  - action: light.turn_on" in diff_text

    # Case 2: Candidate fails validation with breaking errors -> rejected (None)
    broken_report = CompatibilityReport(
        severity=IncompatibilitySeverity.BREAKING, errors=["Fatal schema violation"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=broken_report),
    )

    result_broken = await coordinator.async_generate_modernized_candidate(
        rel_path, full_path, content, FunctionalDomain.AUTOMATION
    )
    assert result_broken is None

    # Case 3: Content unchanged -> returns None
    unchanged_content = "blueprint:\n  name: Modern\n  domain: automation\n"
    result_unchanged = await coordinator.async_generate_modernized_candidate(
        rel_path, full_path, unchanged_content, FunctionalDomain.AUTOMATION
    )
    assert result_unchanged is None


async def test_compatibility_inspection_and_urls(coordinator):
    """Test compatibility inspection and multi-link URL resolution."""
    rel_path = "automation/test.yaml"
    full_path = "/config/blueprints/automation/test.yaml"

    # Test broken YAML
    broken_content = "blueprint: [unclosed mapping"
    report_broken = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, full_path, broken_content
    )
    assert report_broken.severity == IncompatibilitySeverity.BREAKING
    assert any("Invalid YAML" in e for e in report_broken.errors)

    # Test URL resolution
    test_report = CompatibilityReport(
        errors=["Service keyword removed"],
        renamed_keys={"service": "action"},
    )
    primary, author, docs = coordinator._resolve_learn_more_url(
        "https://github.com/home-assistant/blueprints/blob/main/test.yaml",
        test_report,
        "2025.1.0",
    )
    assert author == "https://github.com/home-assistant/blueprints/issues"
    assert docs == "https://www.home-assistant.io/blog/2024/08/07/release-20248/"
    assert primary == docs

    # Community forum URL
    _primary_forum, author_forum, _ = coordinator._resolve_learn_more_url(
        "https://community.home-assistant.io/t/awesome-blueprint/12345",
        test_report,
        "2025.1.0",
    )
    assert author_forum == "https://community.home-assistant.io/t/12345"

    # Gist URL (gist.github.com)
    _primary_gist, author_gist, _ = coordinator._resolve_learn_more_url(
        "https://gist.github.com/test_author/1234567890abcdef",
        test_report,
        "2025.1.0",
    )
    assert author_gist == "https://gist.github.com/1234567890abcdef#comments"

    # Raw Gist URL (gist.githubusercontent.com)
    _primary_gist_raw, author_gist_raw, _ = coordinator._resolve_learn_more_url(
        "https://gist.githubusercontent.com/test_author/1234567890abcdef/raw/blueprint.yaml",
        test_report,
        "2025.1.0",
    )
    assert author_gist_raw == "https://gist.github.com/1234567890abcdef#comments"


async def test_auto_pin_and_auto_unpin_state_machine(coordinator, monkeypatch):
    """Test auto-pinning on fix and auto-unpinning on compatible author release."""
    path = "/config/blueprints/automation/motion.yaml"
    rel_path = "automation/motion.yaml"

    coordinator.data = {
        path: {
            "relative_path": rel_path,
            "local_hash": "local_patched_hash",
            "remote_hash": "upstream_broken_hash",
            "pinned": True,
            "upstream_incompatible_hash": "upstream_broken_hash",
        }
    }
    coordinator._persisted_metadata = {
        rel_path: {
            "pinned": True,
            "upstream_incompatible_hash": "upstream_broken_hash",
        }
    }

    # 1. While remote hash matches upstream_incompatible_hash, update is suppressed
    prev_data = coordinator.data[path]
    info = {"local_hash": "local_patched_hash", "relative_path": rel_path}
    is_updatable, _, _, _ = coordinator._apply_ghost_update_detection(path, info, prev_data)
    assert is_updatable is False

    # 2. Author pushes a new commit! Remote hash changes
    new_remote_content = (
        "blueprint:\n"
        "  name: Motion\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )

    # Case A: Author's new commit is STILL broken
    broken_report = CompatibilityReport(
        severity=IncompatibilitySeverity.BREAKING, errors=["Still broken"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=broken_report),
    )

    await coordinator._process_blueprint_content(
        path,
        coordinator.data[path],
        new_remote_content,
        "https://github.com/author/motion.yaml",
        [],
        set(),
    )
    # Stays pinned
    assert coordinator._persisted_metadata[rel_path]["pinned"] is True
    assert (
        coordinator._persisted_metadata[rel_path]["upstream_incompatible_hash"]
        != "upstream_broken_hash"
    )

    # Case A2: Author's new commit has DEPRECATION severity (still incompatible)
    depr_report = CompatibilityReport(
        severity=IncompatibilitySeverity.DEPRECATION, warnings=["Deprecated service syntax"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=depr_report),
    )
    unpin_spy = AsyncMock(side_effect=coordinator.async_unpin_blueprint)
    monkeypatch.setattr(coordinator, "async_unpin_blueprint", unpin_spy)

    depr_remote_content = (
        "blueprint:\n"
        "  name: Motion Deprecated\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )
    await coordinator._process_blueprint_content(
        path,
        coordinator.data[path],
        depr_remote_content,
        "https://github.com/author/motion.yaml",
        [],
        set(),
    )
    # Stays pinned, non-updatable, and unpin was not called
    assert coordinator._persisted_metadata[rel_path]["pinned"] is True
    assert coordinator.data[path]["updatable"] is False
    unpin_spy.assert_not_called()

    # Case B: Author fixed compatibility!
    fixed_remote_content = (
        "blueprint:\n"
        "  name: Motion Fixed\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )
    clean_report = CompatibilityReport(severity=None)
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=clean_report),
    )

    await coordinator._process_blueprint_content(
        path,
        coordinator.data[path],
        fixed_remote_content,
        "https://github.com/author/motion.yaml",
        [],
        set(),
    )
    # Auto-unpinned!
    assert "pinned" not in coordinator._persisted_metadata.get(rel_path, {})
    assert "upstream_incompatible_hash" not in coordinator._persisted_metadata.get(rel_path, {})
    unpin_spy.assert_called_once_with(rel_path, path)

    # Case C: Unpinned blueprint with no prior metadata does not insert empty entry
    unpinned_path = "/config/blueprints/automation/clean.yaml"
    unpinned_rel = "automation/clean.yaml"
    coordinator.data[unpinned_path] = {
        "relative_path": unpinned_rel,
        "local_hash": "clean_hash",
        "remote_hash": "clean_hash",
        "pinned": False,
    }
    await coordinator._process_blueprint_content(
        unpinned_path,
        coordinator.data[unpinned_path],
        fixed_remote_content,
        "https://github.com/author/clean.yaml",
        [],
        set(),
    )
    assert unpinned_rel not in coordinator._persisted_metadata
    assert "" not in coordinator._persisted_metadata


async def test_dismissal_lifecycle(coordinator, monkeypatch):
    """Test acknowledgment dismissal and automatic re-evaluation on file/version change."""
    rel_path = "automation/depr.yaml"
    full_path = "/config/blueprints/automation/depr.yaml"

    coordinator._persisted_metadata[rel_path] = {
        "dismissed_warning": {
            "dismissed_at_hash": "hash_v1",
            "dismissed_at_ha_version": "2024.12.0",
            "issue_id": "test_issue",
        }
    }

    # Same hash and version: warning stays dismissed
    depr_report = CompatibilityReport(
        severity=IncompatibilitySeverity.DEPRECATION, warnings=["Deprecated service keyword"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=depr_report),
    )
    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(
            return_value={
                full_path: {
                    "relative_path": rel_path,
                    "content": "content_v1",
                    "domain": FunctionalDomain.AUTOMATION,
                    "local_hash": "hash_v1",
                }
            }
        ),
    )
    issue_mock = MagicMock()
    monkeypatch.setattr(coordinator, "_async_create_incompatibility_issue", issue_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)
    issue_mock.assert_not_called()

    # Blueprint content changed (hash_v2): dismissal cleared and re-evaluated
    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(
            return_value={
                full_path: {
                    "relative_path": rel_path,
                    "content": "content_v2",
                    "domain": FunctionalDomain.AUTOMATION,
                    "local_hash": "hash_v2",
                }
            }
        ),
    )
    await coordinator.async_run_post_update_compatibility_guard(force=True)
    issue_mock.assert_called_once()
    assert "dismissed_warning" not in coordinator._persisted_metadata.get(rel_path, {})


async def test_incompatible_blueprint_repair_flow(coordinator, hass, monkeypatch):
    """Test all steps of IncompatibleBlueprintRepairFlow."""
    issue_id = coordinator.get_incompatible_issue_id(
        "automation/broken.yaml", FunctionalDomain.AUTOMATION
    )
    issue_data = {
        "config_entry_id": coordinator.config_entry.entry_id,
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "path": "/config/blueprints/automation/broken.yaml",
        "relative_path": "automation/broken.yaml",
        "domain": FunctionalDomain.AUTOMATION.value,
        "name": "Broken Blueprint",
        "source_url": "https://github.com/author/broken.yaml",
        "has_auto_fix": "true",
        "candidate_content": "blueprint:\n  name: Fixed\n  domain: automation\n",
        "diff_text": "- service: light\n+ action: light\n",
        "breaks_in_ha_version": "2025.2.0",
        "severity": IncompatibilitySeverity.BREAKING.value,
        "errors": "service keyword removed",
    }

    # 1. Routing check
    flow = await async_create_fix_flow(hass, issue_id, issue_data)
    assert isinstance(flow, IncompatibleBlueprintRepairFlow)

    # 2. Step init
    result_init = await flow.async_step_init()
    assert result_init.get("type") == data_entry_flow.FlowResultType.MENU
    menu_options = result_init.get("menu_options") or []
    assert RepairIncompatibleAction.AUTO_FIX.value in menu_options
    assert RepairIncompatibleAction.ACKNOWLEDGE.value not in menu_options
    assert RepairIncompatibleAction.CHANGE_URL.value in menu_options

    # Verify deprecation severity DOES include acknowledge
    flow.severity = IncompatibilitySeverity.DEPRECATION.value
    result_init_depr = await flow.async_step_init()
    assert RepairIncompatibleAction.ACKNOWLEDGE.value in (
        result_init_depr.get("menu_options") or []
    )

    # 3. Step auto_fix: preview diff and execute
    result_af_form = await flow.async_step_auto_fix()
    assert result_af_form.get("type") == data_entry_flow.FlowResultType.FORM

    monkeypatch.setattr(coordinator, "async_install_blueprint", AsyncMock(return_value=None))
    delete_issue_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_issue_mock)

    result_af_apply = await flow.async_step_auto_fix({"confirm_apply": True})
    assert result_af_apply.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert coordinator._persisted_metadata["automation/broken.yaml"]["pinned"] is True
    delete_issue_mock.assert_called_with(hass, DOMAIN, issue_id)

    # 4. Step unpin
    coordinator._persisted_metadata["automation/broken.yaml"]["upstream_incompatible_hash"] = (
        "bad_hash"
    )
    result_unpin_form = await flow.async_step_unpin()
    assert result_unpin_form.get("type") == data_entry_flow.FlowResultType.FORM
    result_unpin = await flow.async_step_unpin({"confirm_unpin": True})
    assert result_unpin.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert "pinned" not in coordinator._persisted_metadata.get("automation/broken.yaml", {})
    assert "upstream_incompatible_hash" not in coordinator._persisted_metadata.get(
        "automation/broken.yaml", {}
    )

    # 5. Step acknowledge
    result_ack_form = await flow.async_step_acknowledge()
    assert result_ack_form.get("type") == data_entry_flow.FlowResultType.FORM
    result_ack = await flow.async_step_acknowledge({"confirm_acknowledge": True})
    assert result_ack.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert "dismissed_warning" in coordinator._persisted_metadata["automation/broken.yaml"]


async def test_repair_flow_change_url_fork_parity(coordinator, hass, monkeypatch):
    """Test switching to a community fork and handling deprecation parity."""
    issue_id = coordinator.get_incompatible_issue_id(
        "automation/fork_test.yaml", FunctionalDomain.AUTOMATION
    )
    issue_data = {
        "config_entry_id": coordinator.config_entry.entry_id,
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "path": "/config/blueprints/automation/fork_test.yaml",
        "relative_path": "automation/fork_test.yaml",
        "domain": FunctionalDomain.AUTOMATION.value,
        "name": "Fork Test",
        "source_url": "https://github.com/original/fork_test.yaml",
        "has_auto_fix": "true",
        "candidate_content": "",
        "diff_text": "",
        "breaks_in_ha_version": "2025.3.0",
        "severity": IncompatibilitySeverity.DEPRECATION.value,
        "warnings": "Deprecated service syntax",
    }

    flow = IncompatibleBlueprintRepairFlow(coordinator, issue_id, issue_data)

    # Mock coordinator fetching fork data safely
    monkeypatch.setattr(
        coordinator,
        "async_fetch_import_data",
        AsyncMock(
            return_value=(
                "blueprint:\n  name: Forked\n  domain: automation\n",
                "https://github.com/parity_fork/fork.yaml",
                "author",
                "Forked",
                MagicMock(),
            )
        ),
    )

    # Case A: Fork has breaking error -> rejected
    breaking_report = CompatibilityReport(
        severity=IncompatibilitySeverity.BREAKING, errors=["Syntax error in fork"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=breaking_report),
    )
    result_breaking = await flow.async_step_change_url(
        {"url": "https://github.com/broken_fork/fork.yaml"}
    )
    assert result_breaking.get("type") == data_entry_flow.FlowResultType.FORM
    assert (result_breaking.get("errors") or {}).get("base") == "fork_breaking_error"

    # Case B: Fork has same deprecation warning -> displays parity notice
    parity_report = CompatibilityReport(
        severity=IncompatibilitySeverity.DEPRECATION, warnings=["Deprecated service syntax"]
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=parity_report),
    )
    monkeypatch.setattr(
        BlueprintUpdateCoordinator,
        "_read_blueprint_file",
        MagicMock(return_value=("old_content", "old_hash")),
    )
    monkeypatch.setattr(
        BlueprintFileStore,
        "capture_precondition",
        MagicMock(return_value=FileRevisionPrecondition.existing("old_hash")),
    )

    result_confirm = await flow.async_step_change_url(
        {"url": "https://github.com/parity_fork/fork.yaml"}
    )
    assert result_confirm.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_confirm.get("step_id") == "confirm_fork"
    placeholders = result_confirm.get("description_placeholders") or {}
    assert "Fork Contains the Same Deprecation Warning" in str(placeholders.get("fork_notice"))

    # Proceed with fork
    monkeypatch.setattr(coordinator, "async_install_blueprint", AsyncMock(return_value=None))
    delete_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_mock)

    coordinator._persisted_metadata["automation/fork_test.yaml"] = {
        "upstream_incompatible_hash": "bad_hash"
    }
    result_exec = await flow.async_step_confirm_fork(
        {"fork_action": RepairForkAction.PROCEED.value}
    )
    assert result_exec.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert (
        coordinator._persisted_metadata["automation/fork_test.yaml"]["source_url"]
        == "https://github.com/parity_fork/fork.yaml"
    )
    assert (
        "upstream_incompatible_hash"
        not in coordinator._persisted_metadata["automation/fork_test.yaml"]
    )
    delete_mock.assert_called_with(hass, DOMAIN, issue_id)


async def test_incompatible_repair_flow_edge_cases(coordinator, hass, monkeypatch):
    """Test edge cases, aborts, and input validation in IncompatibleBlueprintRepairFlow."""
    issue_id = "test_incompatible_issue"

    # Case 1: Missing path/relative_path -> abort
    flow_missing = IncompatibleBlueprintRepairFlow(
        coordinator, issue_id, {"relative_path": "", "path": ""}
    )
    result_abort = await flow_missing.async_step_init()
    assert result_abort.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_abort.get("reason") == "missing_issue_data"

    issue_data = {
        "config_entry_id": coordinator.config_entry.entry_id,
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "path": "/config/blueprints/automation/test.yaml",
        "relative_path": "automation/test.yaml",
        "domain": FunctionalDomain.AUTOMATION.value,
        "has_auto_fix": "true",
        "candidate_content": "modern content",
        "diff_text": "diff",
    }
    flow = IncompatibleBlueprintRepairFlow(coordinator, issue_id, issue_data)

    # Case 2: Auto-fix unconfirmed -> validation error
    result_af_no_confirm = await flow.async_step_auto_fix({"confirm_apply": False})
    assert (result_af_no_confirm.get("errors") or {}).get("base") == "confirmation_required"

    # Case 3: Auto-fix install fails -> patch_failed abort
    monkeypatch.setattr(
        coordinator,
        "async_install_blueprint",
        AsyncMock(side_effect=RuntimeError("Disk write failed")),
    )
    result_af_fail = await flow.async_step_auto_fix({"confirm_apply": True})
    assert result_af_fail.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_af_fail.get("reason") == "patch_failed"

    # Case 4: Acknowledge unconfirmed -> validation error
    result_ack_no_confirm = await flow.async_step_acknowledge({"confirm_acknowledge": False})
    assert (result_ack_no_confirm.get("errors") or {}).get("base") == "confirmation_required"

    # Case 5: Unpin unconfirmed -> validation error
    result_unpin_no_confirm = await flow.async_step_unpin({"confirm_unpin": False})
    assert (result_unpin_no_confirm.get("errors") or {}).get("base") == "confirmation_required"

    # Case 6: Change URL empty -> missing_url
    result_url_empty = await flow.async_step_change_url({"url": ""})
    assert (result_url_empty.get("errors") or {}).get("url") == "missing_url"

    # Case 7: Change URL client fetch error -> invalid_url
    monkeypatch.setattr(
        coordinator,
        "async_fetch_import_data",
        AsyncMock(side_effect=HomeAssistantError("Network connection failed")),
    )
    result_url_err = await flow.async_step_change_url(
        {"url": "https://invalid.example.com/bp.yaml"}
    )
    assert (result_url_err.get("errors") or {}).get("url") == "invalid_url"

    # Case 8: Confirm fork routing to auto-fix and different URL
    flow._pending_url = "https://example.com/fork.yaml"
    flow._pending_content = "content"
    flow._fork_candidate = ("modern content", "- old\n+ modern")
    flow._pending_precondition = FileRevisionPrecondition.existing("precondition_hash")
    result_fork_to_af = await flow.async_step_confirm_fork(
        {"fork_action": RepairForkAction.AUTO_FIX.value}
    )
    assert result_fork_to_af.get("step_id") == "auto_fix"
    assert flow.candidate_content == "modern content"
    assert flow.diff_text == "- old\n+ modern"

    # When no candidate can be generated, AUTO_FIX proceeds with fork switch
    flow._fork_candidate = None
    monkeypatch.setattr(coordinator, "async_install_blueprint", AsyncMock(return_value=None))
    result_fork_no_candidate = await flow.async_step_confirm_fork(
        {"fork_action": RepairForkAction.AUTO_FIX.value}
    )
    assert result_fork_no_candidate.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY

    result_fork_to_diff_url = await flow.async_step_confirm_fork(
        {"fork_action": RepairForkAction.DIFFERENT_URL.value}
    )
    assert result_fork_to_diff_url.get("step_id") == "change_url"

    # Case 9: Fork install failure -> error in form
    monkeypatch.setattr(
        coordinator,
        "async_install_blueprint",
        AsyncMock(side_effect=RuntimeError("Install failed")),
    )
    result_fork_fail = await flow.async_step_confirm_fork(
        {"fork_action": RepairForkAction.PROCEED.value}
    )
    assert (result_fork_fail.get("errors") or {}).get("base") == "Install failed"

    # Case 10: Auto-fix aborts with file_changed on FileRevisionMismatchError
    monkeypatch.setattr(
        coordinator,
        "async_install_blueprint",
        AsyncMock(side_effect=FileRevisionMismatchError("Revision mismatch")),
    )
    flow.candidate_source_file_hash = "stale_hash"
    result_af_file_changed = await flow.async_step_auto_fix({"confirm_apply": True})
    assert result_af_file_changed.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_af_file_changed.get("reason") == "file_changed"

    # Case 11: Fork switch updates cached source_url before installation and passes source_url
    install_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_install_blueprint", install_mock)
    coordinator.data[flow.path] = {"source_url": "https://example.com/old.yaml"}
    flow._pending_url = "https://example.com/fork_updated.yaml"
    flow._pending_content = "blueprint:\n  name: Fork\n"
    result_fork_switch_url = await flow._async_execute_fork_switch()
    assert result_fork_switch_url.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert coordinator.data[flow.path]["source_url"] == "https://example.com/fork_updated.yaml"
    install_mock.assert_called_once()
    assert (
        install_mock.call_args.kwargs.get("source_url") == "https://example.com/fork_updated.yaml"
    )


async def test_async_create_and_delete_incompatibility_issue(coordinator, hass):
    """Test issue creation and deletion behaviors."""
    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"

    report_breaking = CompatibilityReport(
        severity=IncompatibilitySeverity.BREAKING,
        errors=["Service call deprecated"],
        warnings=["Some warning"],
        affected_entities=["automation.kitchen_lights"],
        breaks_in_ha_version="2025.1.0",
        author_report_url="https://github.com/author/repo/issues",
        ha_docs_url="https://www.home-assistant.io/docs",
    )
    info = {
        "relative_path": rel_path,
        "name": "Test BP",
        "domain": FunctionalDomain.AUTOMATION,
        "source_url": "https://github.com/author/repo/blob/main/test.yaml",
    }

    # 1. Breaking issue creation with auto-fix candidate
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create:
        coordinator._async_create_incompatibility_issue(
            path, report_breaking, info=info, candidate=("new content", "diff content")
        )
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["domain"] == DOMAIN
        assert kwargs["severity"] == ir.IssueSeverity.ERROR
        assert kwargs["data"]["has_auto_fix"] == "true"
        assert kwargs["data"]["candidate_content"] == "new content"
        assert kwargs["data"]["diff_text"] == "diff content"

    # 2. Deprecation issue creation without candidate
    report_depr = CompatibilityReport(
        severity=IncompatibilitySeverity.DEPRECATION,
        warnings=["Deprecated syntax"],
    )
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create_depr:
        coordinator._async_create_incompatibility_issue(path, report_depr, info=info)
        mock_create_depr.assert_called_once()
        _, kwargs = mock_create_depr.call_args
        assert kwargs["severity"] == ir.IssueSeverity.WARNING
        assert kwargs["data"]["has_auto_fix"] == "false"

    # 3. None severity -> no-op
    report_none = CompatibilityReport(severity=None)
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create_none:
        coordinator._async_create_incompatibility_issue(path, report_none, info=info)
        mock_create_none.assert_not_called()

    # 4. Missing relative path -> no-op
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
            return_value=None,
        ),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create_no_path,
    ):
        coordinator._async_create_incompatibility_issue(
            "invalid_path", report_breaking, info={"relative_path": ""}
        )
        mock_create_no_path.assert_not_called()

    # 5. Delete issue
    coordinator.data = {path: {"relative_path": rel_path, "domain": "automation"}}
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_del:
        coordinator._async_delete_incompatibility_issue(path)
        mock_del.assert_called_once_with(
            hass,
            DOMAIN,
            coordinator.get_incompatible_issue_id(rel_path, FunctionalDomain.AUTOMATION),
        )


async def test_async_validate_local_blueprint_compatibility_deep(coordinator, monkeypatch):
    """Test deep code paths in async_validate_local_blueprint_compatibility."""
    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"

    # 1. Non-dict root
    report_list = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, path, "- item1\n- item2\n"
    )
    assert report_list.severity == IncompatibilitySeverity.BREAKING
    assert any("mapping" in e for e in report_list.errors)

    # 2. Template syntax error
    invalid_template = (
        "blueprint:\n"
        "  name: Bad Template\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    data:\n"
        '      brightness: "{{ unclosed template "\n'
    )
    report_tmpl = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, path, invalid_template
    )
    assert report_tmpl.severity == IncompatibilitySeverity.BREAKING
    assert any("Template compatibility error" in e for e in report_tmpl.errors)

    # 3. Schema error / Baseline validation error
    invalid_schema_content = (
        "blueprint:\n"
        "  name: Bad Action\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - invalid_action: light.turn_on\n"
    )

    async def _mock_baseline_fail(*args, **kwargs):
        """Simulate baseline validation failure."""
        raise vol.Invalid("Invalid action key")

    monkeypatch.setattr(coordinator, "_async_run_baseline_validation", _mock_baseline_fail)
    report_schema = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, path, invalid_schema_content
    )
    assert report_schema.severity == IncompatibilitySeverity.BREAKING
    assert any("Baseline validation failed" in e for e in report_schema.errors)

    # 4. Valid blueprint with baseline validation success and captured diagnostics
    valid_content = (
        "blueprint:\n"
        "  name: Valid Motion\n"
        "  domain: automation\n"
        "  source_url: https://github.com/author/motion/blob/main/motion.yaml\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )

    async def _mock_baseline(blueprint_dict, blueprint_obj, rel_path, domain, diagnostics):
        """Simulate baseline validation capturing diagnostics."""
        diagnostics.deprecated_keys.append("legacy_key")
        diagnostics.renamed_keys["service"] = "action"
        diagnostics.reports.append(
            (
                "test_report",
                {"what": "Deprecated platform", "breaks_in_ha_version": "2025.5.0"},
            )
        )

    monkeypatch.setattr(coordinator, "_async_run_baseline_validation", _mock_baseline)

    report_valid = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, path, valid_content
    )
    assert report_valid.severity == IncompatibilitySeverity.DEPRECATION
    assert report_valid.breaks_in_ha_version == "2025.5.0"
    assert "Deprecated platform" in report_valid.warnings
    assert report_valid.author_report_url == "https://github.com/author/motion/issues"
    assert report_valid.ha_docs_url is not None


async def test_async_validate_local_blueprint_compatibility_consumers(coordinator, monkeypatch):
    """Test consumer substitution and validation handling in compatibility inspection."""
    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"
    content = (
        "blueprint:\n"
        "  name: Consumer BP\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )

    # Mock consumers
    monkeypatch.setattr(
        coordinator,
        "_get_blueprint_consumers",
        MagicMock(
            return_value=[
                "automation.consumer_1",
                "automation.consumer_2",
                "automation.consumer_3",
            ]
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_get_entities_configs",
        MagicMock(
            return_value={
                "automation.consumer_1": {"use_blueprint": {"path": rel_path, "input": {}}},
                "automation.consumer_2": {"use_blueprint": {"path": rel_path, "input": {}}},
                "automation.consumer_3": {"use_blueprint": {"path": rel_path, "input": {}}},
            }
        ),
    )

    # Consumer 1 succeeds and rewrites service to action
    # Consumer 2 raises vol.Invalid with path
    # Consumer 3 raises general Exception
    async def _mock_run_domain(domain, eid, sub_cfg):
        """Simulate domain validation for consumer entities."""
        if eid == "automation.consumer_1":
            return {
                "action": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.living_room"},
                    }
                ]
            }
        if eid == "automation.consumer_2":
            raise vol.Invalid("Invalid target", path=["action", 0, "target"])
        raise RuntimeError("Crash in validator")

    monkeypatch.setattr(coordinator, "_async_run_domain_validator", _mock_run_domain)

    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)
    assert report.severity == IncompatibilitySeverity.BREAKING
    assert any(
        "automation.consumer_2: At action -> 0 -> target: Invalid target" in e
        for e in report.errors
    )
    assert any(
        "automation.consumer_3: Validation error: Crash in validator" in e for e in report.errors
    )
    assert report.affected_entities == [
        "automation.consumer_1",
        "automation.consumer_2",
        "automation.consumer_3",
    ]


async def test_async_validate_diagnostics_and_errors(coordinator, monkeypatch):
    """Test diagnostics exceptions and new issues reporting."""
    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"
    content = (
        "blueprint:\n"
        "  name: Diagnostics BP\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )

    # 1. capture_structural_validation_diagnostics exception
    class _FailingContext:
        """Context manager simulating a diagnostics failure."""

        async def __aenter__(self):
            """Raise runtime error on enter."""
            raise RuntimeError("Capture context failure")

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            """Exit the context manager."""

    with patch(
        "custom_components.blueprints_updater.coordinator.capture_structural_validation_diagnostics",
        return_value=_FailingContext(),
    ):
        report_fail = await coordinator.async_validate_local_blueprint_compatibility(
            rel_path, path, content
        )
        assert report_fail.severity == IncompatibilitySeverity.BREAKING
        assert any("Diagnostics error: Capture context failure" in e for e in report_fail.errors)

    # 2. Diagnostics with new_issues reporting
    class _MockIssue:
        """Mock issue with version and issue id."""

        breaks_in_ha_version = "2025.4.0"
        issue_id = "deprecated_action_issue"

    async def _mock_baseline_issue(blueprint_dict, blueprint_obj, rel_path, domain, diagnostics):
        """Simulate baseline validation appending a new issue."""
        diagnostics.new_issues.append(_MockIssue())

    monkeypatch.setattr(coordinator, "_async_run_baseline_validation", _mock_baseline_issue)
    report_issue = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, path, content
    )
    assert report_issue.severity == IncompatibilitySeverity.DEPRECATION
    assert report_issue.breaks_in_ha_version == "2025.4.0"
    assert any("Home Assistant issue: deprecated_action_issue" in w for w in report_issue.warnings)


async def test_modernize_preserves_multiline_and_plain_strings() -> None:
    """Test modernize_legacy_blueprint_yaml preserves multiline scalars and non-template text."""
    content = (
        "blueprint:\n"
        "  name: Test Multiline BP\n"
        '  description: "Documentation: use math.floor and | float safely."\n'
        "  domain: automation\n"
        "# Note: service: and platform: should not be replaced in comments\n"
        "action:\n"
        "  - action: notify.notify\n"
        "    data:\n"
        "      message: |\n"
        "        Here is example text:\n"
        "        service: light.turn_on\n"
        "        platform: state\n"
        "        Template inside multiline: {{ math.floor(states('sensor.x') | float * 2) }}\n"
        "  - service: light.turn_on\n"
        "    entity_id: light.kitchen\n"
    )

    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)
    # Plain text description preserved
    assert "use math.floor and | float safely." in modernized
    # Comments preserved
    assert "# Note: service: and platform: should not be replaced in comments" in modernized
    # Multiline plain scalar lines preserved
    assert "        service: light.turn_on\n" in modernized
    assert "        platform: state\n" in modernized
    # Jinja template inside multiline modernized
    assert "(states('sensor.x') | float(0) * 2) | round(0, \"floor\")" in modernized
    # Actual action outside multiline modernized
    assert "  - action: light.turn_on\n" in modernized


async def test_async_generate_modernized_candidate_roundtrip_validation(
    coordinator, monkeypatch
) -> None:
    """Test modernized candidate generation rejects candidates failing round-trip validation."""
    rel_path = "automation/test.yaml"
    full_path = "/config/blueprints/automation/test.yaml"
    content = (
        "blueprint:\n  name: Test\n  domain: automation\naction:\n  - service: light.turn_on\n"
    )

    # 1. Invalid YAML candidate is rejected
    with patch(
        "custom_components.blueprints_updater.coordinator.modernize_legacy_blueprint_yaml",
        return_value="invalid: yaml: [unbalanced",
    ):
        candidate = await coordinator.async_generate_modernized_candidate(
            rel_path, full_path, content, FunctionalDomain.AUTOMATION
        )
        assert candidate is None

    # 2. Candidate that corrupts blueprint section is rejected by structural round-trip
    corrupted_bp = (
        "blueprint:\n"
        "  name: Corrupted Name\n"
        "  domain: automation\n"
        "action:\n"
        "  - action: light.turn_on\n"
    )
    with patch(
        "custom_components.blueprints_updater.coordinator.modernize_legacy_blueprint_yaml",
        return_value=corrupted_bp,
    ):
        candidate = await coordinator.async_generate_modernized_candidate(
            rel_path, full_path, content, FunctionalDomain.AUTOMATION
        )
        assert candidate is None

    # 3. Candidate that corrupts any invariant top-level key is rejected
    from custom_components.blueprints_updater.const import BLUEPRINT_ROUNDTRIP_INVARIANT_KEYS

    for inv_key in BLUEPRINT_ROUNDTRIP_INVARIANT_KEYS:
        inv_content = (
            "blueprint:\n"
            "  name: Test\n"
            "  domain: automation\n"
            f"{inv_key}: original\n"
            "action:\n"
            "  - service: light.turn_on\n"
        )
        corrupted_inv = (
            "blueprint:\n"
            "  name: Test\n"
            "  domain: automation\n"
            f"{inv_key}: corrupted\n"
            "action:\n"
            "  - action: light.turn_on\n"
        )
        with patch(
            "custom_components.blueprints_updater.coordinator.modernize_legacy_blueprint_yaml",
            return_value=corrupted_inv,
        ):
            candidate = await coordinator.async_generate_modernized_candidate(
                rel_path, full_path, inv_content, FunctionalDomain.AUTOMATION
            )
            assert candidate is None


async def test_capture_structural_validation_diagnostics_isolation(hass, monkeypatch) -> None:
    """Test diagnostics capture ignores calls from other tasks and isolates issue registry."""
    import asyncio
    from unittest.mock import MagicMock

    from homeassistant.core import CoreState
    from homeassistant.helpers import frame

    hass.state = CoreState.running
    hass.loop = asyncio.get_running_loop()
    registry = ir.async_get(hass)
    monkeypatch.setattr(registry, "async_schedule_save", MagicMock())
    ir.async_create_issue(
        hass,
        "existing_domain",
        "existing_issue",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="existing",
    )

    base_report = MagicMock()
    monkeypatch.setattr(frame, "report_usage", base_report)

    async with capture_structural_validation_diagnostics(hass) as diagnostics:
        frame.report_usage("validation deprecation", breaks_in_ha_version="2025.6.0")
        ir.async_create_issue(
            hass,
            "automation",
            "validation_task_issue",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="val_issue",
        )
        ir.async_create_issue(
            hass,
            "existing_domain",
            "existing_issue",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="existing_mutated",
        )

        async def _unrelated_task() -> None:
            """Simulate an unrelated background task in Home Assistant."""
            frame.report_usage("unrelated integration usage")
            ir.async_create_issue(
                hass,
                "unrelated_domain",
                "unrelated_issue",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="unrelated",
            )

        task = asyncio.create_task(_unrelated_task())
        await task

    # Current task's report captured, unrelated task's report ignored
    captured_whats = [item[1]["what"] for item in diagnostics.reports]
    assert "validation deprecation" in captured_whats
    assert "unrelated integration usage" not in captured_whats
    assert base_report.call_count == 2

    # Validation task issue captured in new_issues and deleted from registry
    assert any(i.issue_id == "validation_task_issue" for i in diagnostics.new_issues)
    assert ("automation", "validation_task_issue") not in registry.issues

    # Existing issue not captured in new_issues and original state restored
    assert all(i.issue_id != "existing_issue" for i in diagnostics.new_issues)
    assert ("existing_domain", "existing_issue") in registry.issues
    existing_entry = registry.issues[("existing_domain", "existing_issue")]
    assert existing_entry.severity == ir.IssueSeverity.WARNING
    assert existing_entry.translation_key == "existing"

    # Unrelated task issue preserved in registry
    assert ("unrelated_domain", "unrelated_issue") in registry.issues


@pytest.mark.parametrize("validation_error", [None, RuntimeError, asyncio.CancelledError])
async def test_capture_diagnostics_restores_issue_notifications(hass, validation_error) -> None:
    """Test restored issues are published and saved even when validation is interrupted."""
    hass.loop = asyncio.get_running_loop()
    registry = ir.async_get(hass)
    with patch.object(registry, "async_schedule_save") as save:
        for issue_id in ("first", "second"):
            ir.async_create_issue(
                hass,
                "automation",
                issue_id,
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="original",
            )
        snapshot = dict(registry.issues)
        observed_entries = []

        def observe_restoration(event_type, event_data) -> None:
            """Read restored issue data when the notification is published."""
            assert event_type == ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED
            assert event_data["action"] == "update"
            key = (event_data["domain"], event_data["issue_id"])
            observed_entries.append(registry.issues[key])

        with (
            pytest.raises(validation_error) if validation_error else nullcontext(),
            patch.object(hass.bus, "async_fire", side_effect=observe_restoration),
        ):
            async with capture_structural_validation_diagnostics(hass) as diagnostics:
                for issue_id in ("first", "second", "first"):
                    ir.async_create_issue(
                        hass,
                        "automation",
                        issue_id,
                        is_fixable=False,
                        is_persistent=True,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="temporary",
                    )
                observed_entries.clear()
                save.reset_mock()
                if validation_error:
                    raise validation_error()

        assert observed_entries == list(snapshot.values())
        assert registry.issues == snapshot
        assert diagnostics.new_issues == []
        save.assert_called_once_with()


async def test_setup_entry_ha_running_triggers_compatibility_guard(hass) -> None:
    """Test async_setup_entry schedules compatibility guard immediately when HA is running."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    entry.async_on_unload = MagicMock()

    hass.state = CoreState.running
    hass.is_running = True
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.bus.async_listen_once = MagicMock()

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_schedule_post_update_compatibility_guard = MagicMock()
    coordinator_mock.data = {}

    with patch(
        "custom_components.blueprints_updater.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        assert await async_setup_entry(hass, entry) is True

    coordinator_mock.async_schedule_post_update_compatibility_guard.assert_called_once()
    hass.bus.async_listen_once.assert_not_called()


async def test_setup_entry_ha_not_running_waits_for_startup_event(hass) -> None:
    """Test async_setup_entry registers startup listener when HA is not running."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    entry.async_on_unload = MagicMock()

    hass.state = CoreState.not_running
    hass.is_running = False
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    startup_listener = None

    def _mock_listen_once(event_type, callback):
        """Capture registered startup listener."""
        nonlocal startup_listener
        startup_listener = callback
        return MagicMock()

    hass.bus.async_listen_once = MagicMock(side_effect=_mock_listen_once)

    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_schedule_post_update_compatibility_guard = MagicMock()
    coordinator_mock.data = {}

    with patch(
        "custom_components.blueprints_updater.BlueprintUpdateCoordinator",
        return_value=coordinator_mock,
    ):
        assert await async_setup_entry(hass, entry) is True

    assert startup_listener is not None
    coordinator_mock.async_schedule_post_update_compatibility_guard.assert_not_called()

    # Simulate HA startup event firing
    startup_listener(None)
    coordinator_mock.async_schedule_post_update_compatibility_guard.assert_called_once()


async def test_schedule_post_update_guard_disabled(coordinator) -> None:
    """Test scheduling compatibility guard when verify_on_ha_update is disabled."""
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: False})
    task = coordinator.async_schedule_post_update_compatibility_guard()
    assert task is None
    assert coordinator._post_ha_update_task is None


async def test_schedule_post_update_guard_lifecycle_and_deduplication(coordinator) -> None:
    """Test scheduling compatibility guard deduplication, completion, and cleanup."""
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})
    guard_executed = False

    async def _mock_guard(force: bool = False) -> None:
        """Mock compatibility guard execution."""
        nonlocal guard_executed
        guard_executed = True

    coordinator.async_run_post_update_compatibility_guard = _mock_guard
    task1 = coordinator.async_schedule_post_update_compatibility_guard()
    assert task1 is not None
    assert coordinator._post_ha_update_task is task1

    # Scheduling again while active returns same task
    task2 = coordinator.async_schedule_post_update_compatibility_guard()
    assert task2 is task1

    await task1
    assert guard_executed
    # Reference should be cleared after completion
    assert coordinator._post_ha_update_task is None


async def test_schedule_post_update_guard_error_handling(coordinator) -> None:
    """Test that unexpected exceptions during post-update check are caught and cleared."""
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})

    async def _failing_guard(force: bool = False) -> None:
        """Simulate failing compatibility guard execution."""
        raise RuntimeError("simulated error")

    coordinator.async_run_post_update_compatibility_guard = _failing_guard
    task = coordinator.async_schedule_post_update_compatibility_guard()
    assert task is not None

    # Should complete without propagating the exception to caller
    await task
    assert coordinator._post_ha_update_task is None


async def test_unload_cancels_post_update_guard_task(coordinator) -> None:
    """Test that unloading and cancellation cancels running post-update check."""
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})

    started = asyncio.Event()
    stop_event = asyncio.Event()
    cancelled = False

    async def _long_running(force: bool = False) -> None:
        """Simulate a long-running compatibility guard task."""
        nonlocal cancelled
        started.set()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    coordinator.async_run_post_update_compatibility_guard = _long_running
    task = coordinator.async_schedule_post_update_compatibility_guard()
    assert task is not None
    assert not task.done()

    # Ensure the task has entered execution and reached sleep
    await started.wait()

    # Cancel via coordinator unload hook
    coordinator._async_cancel_background_task()
    assert task.cancelling() > 0 or task.cancelled()

    # Shutdown should also clean up
    await coordinator.async_shutdown()
    assert coordinator._post_ha_update_task is None
    assert cancelled
    assert task.cancelled()


async def test_async_unload_entry_cleans_up_coordinator(hass) -> None:
    """Test that async_unload_entry shuts down coordinator and unloads entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.async_shutdown = AsyncMock()
    hass.data = {DOMAIN: {"coordinators": {entry.entry_id: coordinator_mock}}}
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True
    coordinator_mock.async_shutdown.assert_awaited_once()
    assert entry.entry_id not in hass.data[DOMAIN]["coordinators"]


def test_modernize_math_conversions() -> None:
    """Test modernize_legacy_blueprint_yaml converts math.floor and math.ceil to round filters."""
    content = (
        "blueprint:\n"
        "  name: Math Test\n"
        "  domain: automation\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    data:\n"
        '      val_floor: "{{ math.floor(1.5) }}"\n'
        '      val_ceil: "{{ math.ceil(1.2) }}"\n'
        '      val_nested: "{{ math.floor(math.ceil(2.3)) }}"\n'
        '      val_sin: "{{ math.sin(0.5) }}"\n'
    )
    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)
    assert "val_floor: \"{{ ((1.5) | round(0, 'floor')) }}\"" in modernized
    assert "val_ceil: \"{{ ((1.2) | round(0, 'ceil')) }}\"" in modernized
    assert "val_nested: \"{{ (((2.3) | round(0, 'ceil')) | round(0, 'floor')) }}\"" in modernized
    assert 'val_sin: "{{ sin(0.5) }}"' in modernized
    assert "math.floor" not in modernized
    assert "math.ceil" not in modernized
    assert "math.sin" not in modernized


def test_modernize_math_rejections() -> None:
    """Test modernize_legacy_blueprint_yaml rejects unsafe math candidates."""
    base = (
        "blueprint:\n"
        "  name: Math Rejection Test\n"
        "  domain: automation\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    data:\n"
        '      val: "{}"\n'
    )

    unsafe_cases = [
        "{{ math.floor }}",
        "{{ math.floor() }}",
        "{{ math.floor(1, 2) }}",
        "{{ math.floor(1 }}",
        "{{ math.ceil }}",
        "{{ math.ceil() }}",
        "{{ math.ceil(1, 2) }}",
        "{{ math.unknown(1) }}",
        "{{ math.isclose(1, 2) }}",
    ]

    for expr in unsafe_cases:
        content = base.format(expr)
        modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)
        # Should reject conversion and revert to original content
        assert modernized == content, f"Expected candidate with {expr} to be rejected"


def test_discover_ha_math_capabilities() -> None:
    """Test dynamic discovery of HA Core math capabilities and fallback."""
    ha_globals, ha_round = _discover_ha_math_capabilities()
    assert "sin" in ha_globals
    assert "cos" in ha_globals
    assert "floor" in ha_round
    assert "ceil" in ha_round
    assert "floor" not in ha_globals
    assert "ceil" not in ha_globals

    # Test fallback path when exception occurs
    with patch(
        "homeassistant.helpers.template.TemplateEnvironment",
        side_effect=RuntimeError("Test error"),
    ):
        fb_globals, fb_round = _discover_ha_math_capabilities()
        assert fb_globals == _DEFAULT_MATH_GLOBALS
        assert fb_round == _DEFAULT_MATH_FILTER_METHODS


async def test_validate_blueprint_compatibility_skips_metadata_templates(coordinator) -> None:
    """Test that template syntax in blueprint metadata description is excluded from validation."""
    rel_path = "automation/tmpl_meta.yaml"
    full_path = "/config/blueprints/automation/tmpl_meta.yaml"
    content = (
        "blueprint:\n"
        "  name: Meta Template Test\n"
        "  description: 'Example: {{ invalid syntax here {{'\n"
        "  domain: automation\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: binary_sensor.motion\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
    )
    report = await coordinator.async_validate_local_blueprint_compatibility(
        rel_path, full_path, content
    )
    assert all("Template compatibility error" not in err for err in report.errors)


def test_diff_structural_configs_top_level_plural_keys() -> None:
    """Test diff_structural_configs skips deprecation inference for top-level plural aliases."""
    input_cfg = {
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.test"}],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.test"},
            }
        ],
    }
    validated_cfg = {
        "triggers": [{"platform": "state", "entity_id": "binary_sensor.test"}],
        "actions": [
            {
                "action": "light.turn_on",
                "target": {"entity_id": "light.test"},
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert "trigger" not in diagnostics.deprecated_keys
    assert "action" not in diagnostics.deprecated_keys
    assert "trigger" not in diagnostics.renamed_keys
    assert "action" not in diagnostics.renamed_keys
    # Nested service -> action must be detected
    assert diagnostics.renamed_keys.get("service") == "action"


def test_derive_substituted_baseline_config(hass) -> None:
    """Test _derive_substituted_baseline_config builds config for executable blueprints."""
    from homeassistant.components.blueprint.models import Blueprint

    from custom_components.blueprints_updater.blueprint_validation import (
        get_blueprint_schema,
    )

    schema = get_blueprint_schema("automation")
    valid_dict: dict[str, object] = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {
                "test_entity": {
                    "name": "Entity",
                    "selector": {"entity": {}},
                }
            },
        },
        "trigger": [{"platform": "state", "entity_id": "!input test_entity"}],
        "action": [{"action": "light.turn_on"}],
    }
    bp_obj = Blueprint(valid_dict, schema=schema)
    res = BlueprintUpdateCoordinator._derive_substituted_baseline_config(
        valid_dict, bp_obj, "automation/test.yaml", FunctionalDomain.AUTOMATION
    )
    assert res is not None
    assert "trigger" in res or "triggers" in res

    # Non-executable blueprint returns None
    invalid_dict: dict[str, object] = {
        "blueprint": {"name": "Empty", "domain": "automation"},
    }
    bp_empty = Blueprint(invalid_dict, schema=schema)
    res_none = BlueprintUpdateCoordinator._derive_substituted_baseline_config(
        invalid_dict, bp_empty, "automation/empty.yaml", FunctionalDomain.AUTOMATION
    )
    assert res_none is None


async def test_post_update_guard_does_not_persist_version_on_exception(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that HA version is NOT persisted if an exception occurs during blueprint scan."""
    hass.config.version = "2026.10.0"
    coordinator._last_ha_version = "2026.9.0"
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})

    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(side_effect=RuntimeError("Filesystem scan error")),
    )
    save_spy = AsyncMock(side_effect=coordinator.async_save_ha_version)
    monkeypatch.setattr(coordinator, "async_save_ha_version", save_spy)

    with pytest.raises(RuntimeError, match="Filesystem scan error"):
        await coordinator.async_run_post_update_compatibility_guard()

    save_spy.assert_not_called()
    assert coordinator._last_ha_version == "2026.9.0"


async def test_post_update_guard_does_not_persist_version_on_timeout(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that HA version is NOT persisted if validation times out."""
    hass.config.version = "2026.10.0"
    coordinator._last_ha_version = "2026.9.0"
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})

    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(
            return_value={
                "/config/blueprints/automation/test.yaml": {
                    "relative_path": "automation/test.yaml",
                    "content": "blueprint: ...",
                    "domain": "automation",
                    "local_hash": "hash1",
                }
            }
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(side_effect=TimeoutError()),
    )
    save_spy = AsyncMock(side_effect=coordinator.async_save_ha_version)
    monkeypatch.setattr(coordinator, "async_save_ha_version", save_spy)

    await coordinator.async_run_post_update_compatibility_guard()

    save_spy.assert_not_called()
    assert coordinator._last_ha_version == "2026.9.0"


async def test_post_update_guard_persists_version_on_clean_completion(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that HA version is persisted when all scans and validations succeed cleanly."""
    hass.config.version = "2026.10.0"
    coordinator._last_ha_version = "2026.9.0"
    coordinator.config_entry.options = MappingProxyType({CONF_VERIFY_ON_HA_UPDATE: True})

    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(
            return_value={
                "/config/blueprints/automation/test.yaml": {
                    "relative_path": "automation/test.yaml",
                    "content": "blueprint: ...",
                    "domain": "automation",
                    "local_hash": "hash1",
                }
            }
        ),
    )
    clean_report = CompatibilityReport(severity=None)
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=clean_report),
    )
    save_spy = AsyncMock(side_effect=coordinator.async_save_ha_version)
    monkeypatch.setattr(coordinator, "async_save_ha_version", save_spy)

    await coordinator.async_run_post_update_compatibility_guard()

    save_spy.assert_awaited_once_with("2026.10.0")
    assert coordinator._last_ha_version == "2026.10.0"


def test_detect_unsupported_yaml_constructs() -> None:
    """Test AST detection of unsupported YAML constructs for automated modernization."""
    # Anchors
    anchor_yaml = (
        "blueprint:\n"
        "  name: Anchor\n"
        "variables: &var\n"
        "  val: 1\n"
        "action:\n"
        "  - service: light.turn_on\n"
    )
    assert detect_unsupported_yaml_constructs(anchor_yaml) is not None
    assert "anchors" in str(detect_unsupported_yaml_constructs(anchor_yaml))

    # Aliases
    alias_yaml = "blueprint:\n  name: Alias\naction:\n  - service: light.turn_on\n    data: *var\n"
    assert detect_unsupported_yaml_constructs(alias_yaml) is not None
    assert "alias" in str(detect_unsupported_yaml_constructs(alias_yaml)).lower()

    # Flow-style mapping in action
    flow_action_yaml = "blueprint:\n  name: Flow Action\naction: [{service: light.turn_on}]\n"
    assert detect_unsupported_yaml_constructs(flow_action_yaml) is not None
    assert "flow-style mapping" in str(detect_unsupported_yaml_constructs(flow_action_yaml))

    # Flow-style mapping in trigger
    flow_trigger_yaml = (
        "blueprint:\n"
        "  name: Flow Trigger\n"
        "trigger: [{platform: state, entity_id: light.foo}]\n"
        "action:\n"
        "  - service: light.turn_on\n"
    )
    assert detect_unsupported_yaml_constructs(flow_trigger_yaml) is not None
    assert "flow-style mapping" in str(detect_unsupported_yaml_constructs(flow_trigger_yaml))

    # Invalid YAML syntax
    invalid_yaml = "blueprint:\n  name: [unclosed"
    assert detect_unsupported_yaml_constructs(invalid_yaml) is not None

    # Normal block YAML with flow scalar list
    normal_yaml = (
        "blueprint:\n"
        "  name: Normal\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    target:\n"
        "      entity_id: [light.a, light.b]\n"
    )
    assert detect_unsupported_yaml_constructs(normal_yaml) is None

    # Home Assistant !input custom tag parses successfully without standard
    # PyYAML constructor failure
    input_tag_yaml = (
        "blueprint:\n"
        "  name: HA Input Tag\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    target:\n"
        "      entity_id: !input target_light\n"
    )
    assert detect_unsupported_yaml_constructs(input_tag_yaml) is None


def test_modernize_preserves_variables_and_trigger_variables() -> None:
    """Test modernize_legacy_blueprint_yaml strictly constrains action/trigger transformations."""
    content = (
        "blueprint:\n"
        "  name: Variable Isolation\n"
        "  domain: automation\n"
        "variables:\n"
        '  service: "my_custom_service"\n'
        '  service_template: "tpl_val"\n'
        '  platform: "ios"\n'
        "trigger_variables:\n"
        '  platform: "custom_trigger"\n'
        '  service: "custom_service"\n'
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: input_boolean.test\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    data_template:\n"
        "      brightness: 255\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)

    # In variables and trigger_variables, keys must remain untouched
    assert '  service: "my_custom_service"' in modernized
    assert '  service_template: "tpl_val"' in modernized
    assert '  platform: "ios"' in modernized
    assert '  platform: "custom_trigger"' in modernized
    assert '  service: "custom_service"' in modernized

    # In trigger and action, legacy keywords must be modernized
    assert "  - trigger: state\n" in modernized
    assert "  - action: light.turn_on\n" in modernized
    assert "    data:\n" in modernized

    # Parsed AST round-trip verification
    orig_parsed = yaml_util.parse_yaml(content)
    mod_parsed = yaml_util.parse_yaml(modernized)
    assert isinstance(orig_parsed, dict)
    assert isinstance(mod_parsed, dict)
    assert orig_parsed["variables"] == mod_parsed["variables"]
    assert orig_parsed["trigger_variables"] == mod_parsed["trigger_variables"]
    assert orig_parsed["blueprint"] == mod_parsed["blueprint"]


def test_modernize_target_blocks_isolated_to_action_sections() -> None:
    """Test modernize_legacy_blueprint_yaml constrains target block wrapping to action sections."""
    content = (
        "blueprint:\n"
        "  name: Scalar and Non-Action Preservation\n"
        "  domain: automation\n"
        "  description: |\n"
        "    Example configuration:\n"
        "    action: light.turn_on\n"
        "    entity_id: light.living_room\n"
        "variables:\n"
        "  action: light.turn_on\n"
        "  entity_id: light.kitchen\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    entity_id: light.patio\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)

    # In blueprint description multiline scalar, target should NOT be wrapped
    assert (
        "Example configuration:\n    action: light.turn_on\n    entity_id: light.living_room\n"
    ) in modernized
    # In variables, target should NOT be wrapped
    assert "variables:\n  action: light.turn_on\n  entity_id: light.kitchen\n" in modernized
    # In action section, target MUST be wrapped
    assert (
        "action:\n  - action: light.turn_on\n    target:\n      entity_id: light.patio\n"
    ) in modernized

    parsed = yaml_util.parse_yaml(modernized)
    assert isinstance(parsed, dict)
    assert parsed["variables"] == {"action": "light.turn_on", "entity_id": "light.kitchen"}
    assert parsed["action"][0]["target"] == {"entity_id": "light.patio"}


def test_modernize_handles_quoted_keys() -> None:
    """Test modernize_legacy_blueprint_yaml preserves quotes on quoted keys."""
    content = (
        "blueprint:\n"
        "  name: Quoted Keys\n"
        "  domain: automation\n"
        "trigger:\n"
        '  - "platform": state\n'
        "    entity_id: input_boolean.test\n"
        "action:\n"
        "  - 'service': light.turn_on\n"
        "    'data_template':\n"
        "      brightness: 255\n"
        "    'entity_id': light.living_room\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)

    assert '  - "trigger": state\n' in modernized
    assert "  - 'action': light.turn_on\n" in modernized
    assert "    'data':\n" in modernized
    assert "    target:\n" in modernized
    assert "      'entity_id': light.living_room\n" in modernized

    parsed = yaml_util.parse_yaml(modernized)
    assert isinstance(parsed, dict)
    action_list = parsed["action"]
    assert isinstance(action_list, list)
    first_action = action_list[0]
    assert isinstance(first_action, dict)
    assert first_action["action"] == "light.turn_on"
    target = first_action["target"]
    assert isinstance(target, dict)
    assert target["entity_id"] == "light.living_room"


async def test_async_generate_modernized_candidate_rejects_unsupported_constructs(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test candidate generator rejects blueprints with unsupported constructs like anchors."""
    anchor_content = (
        "blueprint:\n"
        "  name: Anchor Blueprint\n"
        "  domain: automation\n"
        "variables: &v\n"
        "  x: 1\n"
        "action:\n"
        "  - service: light.turn_on\n"
    )
    candidate = await coordinator.async_generate_modernized_candidate(
        "automation/anchor.yaml",
        "/config/blueprints/automation/anchor.yaml",
        anchor_content,
        FunctionalDomain.AUTOMATION,
    )
    assert candidate is None

    flow_content = (
        "blueprint:\n"
        "  name: Flow Blueprint\n"
        "  domain: automation\n"
        "action: [{service: light.turn_on}]\n"
    )
    flow_candidate = await coordinator.async_generate_modernized_candidate(
        "automation/flow.yaml",
        "/config/blueprints/automation/flow.yaml",
        flow_content,
        FunctionalDomain.AUTOMATION,
    )
    assert flow_candidate is None


def test_wrap_action_target_preserves_subsequent_fields() -> None:
    """Test target wrapping retains all fields when target block precedes other fields."""
    content = (
        "blueprint:\n"
        "  name: Existing Target\n"
        "  domain: automation\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
        "    data:\n"
        "      brightness: 150\n"
        "    response_variable: result\n"
        "    continue_on_error: true\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, domain=FunctionalDomain.AUTOMATION)
    assert "action: light.turn_on" in modernized
    assert "target:" in modernized
    assert "brightness: 150" in modernized
    assert "response_variable: result" in modernized
    assert "continue_on_error: true" in modernized
    parsed = yaml_util.parse_yaml(modernized)
    assert isinstance(parsed, dict)
    act = parsed["action"][0]
    assert act["action"] == "light.turn_on"
    assert act["target"]["entity_id"] == "light.living_room"
    assert act["data"]["brightness"] == 150
    assert act["response_variable"] == "result"
    assert act["continue_on_error"] is True


async def test_post_update_compatibility_guard_handles_timeout(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test post-update guard gracefully handles per-blueprint validation timeouts."""
    full_path = "/config/blueprints/automation/slow.yaml"
    rel_path = "automation/slow.yaml"

    monkeypatch.setattr(
        coordinator,
        "async_scan_all_local_blueprint_files",
        AsyncMock(
            return_value={
                full_path: {
                    "relative_path": rel_path,
                    "content": "blueprint:\n  name: Slow\n  domain: automation\n",
                    "domain": FunctionalDomain.AUTOMATION,
                    "local_hash": "slow_hash",
                }
            }
        ),
    )

    async def _slow_validation(*args: object, **kwargs: object) -> None:
        """Simulate a validation that exceeds timeout."""
        raise TimeoutError

    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        _slow_validation,
    )
    issue_mock = MagicMock()
    monkeypatch.setattr(coordinator, "_async_create_incompatibility_issue", issue_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)
    issue_mock.assert_not_called()


def test_empty_math_round_methods_handling() -> None:
    """Test math round pattern handling when supported methods set is empty."""
    from custom_components.blueprints_updater.blueprint_validation import (
        _rewrite_floor_ceil_in_expr,
    )

    with patch(
        "custom_components.blueprints_updater.blueprint_validation._HA_MATH_ROUND_METHODS",
        frozenset(),
    ):
        expr = "math.sin(1.0)"
        result = _rewrite_floor_ceil_in_expr(expr)
        assert result == expr


def test_block_scalar_preservation_uses_ast_not_indentation() -> None:
    """Test block scalar preservation handles explicit indentation indicator."""
    content = (
        "blueprint:\n"
        "  name: Block Scalar Test\n"
        "  domain: automation\n"
        "action:\n"
        "  - choose:\n"
        "      - conditions: []\n"
        "        sequence:\n"
        "          - description: |2\n"
        "              line 1: service: light.turn_on\n"
        "              line 2: platform: state\n"
        "          - service: light.turn_off\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, domain=FunctionalDomain.AUTOMATION)
    assert "line 1: service: light.turn_on" in modernized
    assert "action: light.turn_off" in modernized


def test_modernize_preserves_jinja_in_variables_and_trigger_variables() -> None:
    """Test modernize_legacy_blueprint_yaml preserves Jinja expressions outside action/trigger."""
    content = (
        "blueprint:\n"
        "  name: Jinja Isolation\n"
        "  domain: automation\n"
        "variables:\n"
        "  test_var: \"{{ states('sensor.test') | float }}\"\n"
        '  math_var: "{{ math.custom_func(1.5) }}"\n'
        "trigger_variables:\n"
        '  trig_var: "{{ 10 | int }}"\n'
        "trigger:\n"
        "  - trigger: state\n"
        "    entity_id: sensor.test\n"
        "action:\n"
        "  - action: light.turn_on\n"
        "    data:\n"
        "      brightness: \"{{ states('sensor.bright') | float }}\"\n"
    )
    modernized = modernize_legacy_blueprint_yaml(content, domain=FunctionalDomain.AUTOMATION)
    # Jinja in variables and trigger_variables must remain un-modernized
    assert "test_var: \"{{ states('sensor.test') | float }}\"" in modernized
    assert 'math_var: "{{ math.custom_func(1.5) }}"' in modernized
    assert 'trig_var: "{{ 10 | int }}"' in modernized
    # Jinja in action must be modernized
    assert "brightness: \"{{ states('sensor.bright') | float(0) }}\"" in modernized


def test_derive_value_for_path_indexed_scalar_fields() -> None:
    """Test _derive_value_for_path distinguishes scalar fields inside indexed items from blocks."""
    # Scalar action inside indexed action item
    assert _derive_value_for_path("action[0].action") == "homeassistant.update_entity"
    assert _derive_value_for_path("action[0].service") == "homeassistant.update_entity"
    # Action sequence block vs indexed action item
    assert _derive_value_for_path("action") == []
    assert _derive_value_for_path("action[0]") == {
        "action": "homeassistant.update_entity",
        "target": {"entity_id": "test.dummy"},
    }

    # Scalar trigger inside indexed trigger item
    assert _derive_value_for_path("trigger[0].trigger") == "state"
    assert _derive_value_for_path("trigger[0].platform") == "state"
    # Trigger sequence block vs indexed trigger item
    assert _derive_value_for_path("trigger") == [{"trigger": "state", "entity_id": "test.dummy"}]
    assert _derive_value_for_path("trigger[0]") == {
        "trigger": "state",
        "entity_id": "test.dummy",
    }

    # Scalar condition inside indexed condition item
    assert _derive_value_for_path("condition[0].condition") == "state"
    # Condition sequence block vs indexed condition item
    assert _derive_value_for_path("condition") == [
        {"condition": "state", "entity_id": "test.dummy", "state": "on"}
    ]
    assert _derive_value_for_path("condition[0]") == {
        "condition": "state",
        "entity_id": "test.dummy",
        "state": "on",
    }

    # Block keys inside indexed items must remain list-valued, not scalar
    assert _derive_value_for_path("action[0].sequence") == []
    assert _derive_value_for_path("action[0].then") == []
    assert _derive_value_for_path("action[0].else") == []
    assert _derive_value_for_path("action[0].default") == []
    assert _derive_value_for_path("condition[0].conditions") == [
        {"condition": "state", "entity_id": "test.dummy", "state": "on"}
    ]

    # Variables blocks vs variable named variables
    assert _derive_value_for_path("variables") == {}
    assert _derive_value_for_path("trigger_variables") == {}
    assert _derive_value_for_path("action[0].variables") == {}
    assert _derive_value_for_path("action[0].sequence[0].variables") == {}
    assert _derive_value_for_path("variables.my_var") == "test.dummy"
    assert _derive_value_for_path("variables.variables") == "test.dummy"
    assert _derive_value_for_path("trigger_variables.variables") == "test.dummy"
    assert _derive_value_for_path("action[0].variables.my_var") == "test.dummy"
    assert _derive_value_for_path("action[0].variables.variables") == "test.dummy"


def test_wrap_action_target_blocks_preserves_ancestors_across_comments_and_blank_lines() -> None:
    """Test ancestor tracking preserves payload context across blank and comment lines."""
    lines = [
        "action:\n",
        "  - service: mqtt.publish\n",
        "    data:\n",
        "\n",
        "      # Structural comment line\n",
        "      action: notify.notify\n",
        "      entity_id: notify.admin\n",
    ]
    target_keys = frozenset({"entity_id"})
    wrapped = _wrap_action_target_blocks(lines, target_keys)
    wrapped_text = "".join(wrapped)
    # The action line inside data must NOT be treated as a service call or wrapped with target:
    assert "target:" not in wrapped_text


async def test_diagnostics_interceptor_forwards_args_and_kwargs(hass, monkeypatch) -> None:
    """Test diagnostics interceptor forwards arbitrary args and kwargs and extends exclusions."""
    base_report = MagicMock()
    monkeypatch.setattr(frame, "report_usage", base_report)
    base_create_issue = MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", base_create_issue)

    async with capture_structural_validation_diagnostics(hass) as diagnostics:
        # Call from current task with extra kwargs
        cast(Any, frame.report_usage)(
            "msg", breaks_in_ha_version="2026.1.0", extra_field="test_extra"
        )
        cast(Any, ir.async_create_issue)(
            hass,
            "automation",
            "issue_1",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="trans_key",
            extra_issue_param="custom_val",
        )

        async def _other_task() -> None:
            """Simulate an unrelated background task calling report_usage and async_create_issue."""
            cast(Any, frame.report_usage)(
                "other_msg",
                exclude_integrations={"other_integration"},
                custom_kw="other_val",
            )
            ir.async_create_issue(
                hass,
                "domain_other",
                "issue_other",
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="key_other",
            )

        task = asyncio.create_task(_other_task())
        await task

    # Current task's report captured in diagnostics
    assert len(diagnostics.reports) == 1
    assert diagnostics.reports[0][1]["what"] == "msg"
    assert diagnostics.reports[0][1]["extra_field"] == "test_extra"
    assert diagnostics.reports[0][1]["breaks_in_ha_version"] == "2026.1.0"

    # Both report calls forwarded
    assert base_report.call_count == 2
    # Second call (from other task) forwarded with DOMAIN in exclude_integrations
    other_call_kwargs = base_report.call_args_list[1].kwargs
    assert other_call_kwargs["custom_kw"] == "other_val"
    assert "blueprints_updater" in other_call_kwargs["exclude_integrations"]
    assert "other_integration" in other_call_kwargs["exclude_integrations"]

    # Both issue calls forwarded with extra params
    assert base_create_issue.call_count == 2
    first_issue_kwargs = base_create_issue.call_args_list[0].kwargs
    assert first_issue_kwargs["extra_issue_param"] == "custom_val"


async def test_fork_repair_restores_url_on_failure(coordinator: BlueprintUpdateCoordinator) -> None:
    """Test fork repair restores runtime and persisted source URL on install failure."""
    path = "/config/blueprints/automation/test/bp.yaml"
    rel_path = "automation/test/bp.yaml"
    coordinator.data[path] = {"source_url": "https://example.com/original.yaml"}
    coordinator._persisted_metadata[rel_path] = {"source_url": "https://example.com/original.yaml"}

    issue_data: dict[str, object] = {
        "path": path,
        "relative_path": rel_path,
        "name": "Original Name",
        "source_url": "https://example.com/original.yaml",
    }
    flow = IncompatibleBlueprintRepairFlow(coordinator, "test_issue", issue_data)
    flow._pending_url = "https://example.com/fork.yaml"
    flow._pending_content = "blueprint:\n  name: Fork\n  domain: automation\n"
    flow._pending_precondition = FileRevisionPrecondition.existing("precondition_hash")

    with patch.object(
        coordinator,
        "async_install_blueprint",
        AsyncMock(side_effect=HomeAssistantError("Disk write failed")),
    ):
        result = await flow._async_execute_fork_switch()

    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("errors") == {"base": "Disk write failed"}
    # Source URLs must be restored
    assert coordinator.data[path]["source_url"] == "https://example.com/original.yaml"
    assert coordinator._persisted_metadata[rel_path]["source_url"] == (
        "https://example.com/original.yaml"
    )


def test_modernize_preserves_payload_keys_under_data_target_variables() -> None:
    """Test modernize_legacy_blueprint_yaml preserves keys beneath data, target, and variables."""
    content = (
        "blueprint:\n"
        "  name: Payload Preservation Test\n"
        "  domain: automation\n"
        "action:\n"
        "  - service: notify.notify\n"
        "    data:\n"
        "      service: internal_service_payload\n"
        "      platform: ios\n"
        "      old_custom_key: payload_value\n"
        "    target:\n"
        "      entity_id: light.test\n"
        "      service: target_payload\n"
        "  - service: light.turn_on\n"
        "    old_custom_key: item_level_value\n"
        "trigger:\n"
        "  - platform: event\n"
        "    event_data:\n"
        "      platform: special_event\n"
        "      service: trigger_event_payload\n"
    )
    dynamic_replacements = {
        "old_custom_key": "new_custom_key",
        "service": "action",
        "platform": "trigger",
    }
    modernized = modernize_legacy_blueprint_yaml(
        content,
        domain=FunctionalDomain.AUTOMATION,
        dynamic_replacements=dynamic_replacements,
    )
    # Item-level keys must be modernized
    assert "  - action: notify.notify\n" in modernized
    assert "  - action: light.turn_on\n" in modernized
    assert "    new_custom_key: item_level_value\n" in modernized
    assert "  - trigger: event\n" in modernized

    # Payload keys inside data, target, event_data must NOT be modernized
    assert "      service: internal_service_payload\n" in modernized
    assert "      platform: ios\n" in modernized
    assert "      old_custom_key: payload_value\n" in modernized
    assert "      service: target_payload\n" in modernized
    assert "      platform: special_event\n" in modernized
    assert "      service: trigger_event_payload\n" in modernized


async def test_compatibility_report_renamed_keys_no_warnings_no_severity(
    coordinator: BlueprintUpdateCoordinator, monkeypatch
) -> None:
    """Test renamed_keys are available for modernization without adding warnings or severity."""
    rel_path = "automation/test/clean.yaml"
    path = "/config/blueprints/automation/test/clean.yaml"
    content = (
        "blueprint:\n"
        "  name: Clean Blueprint\n"
        "  domain: automation\n"
        "action:\n"
        "  - action: light.turn_on\n"
    )

    async def _mock_baseline(blueprint_dict, blueprint_obj, rel_path, domain, diagnostics):
        """Simulate baseline capturing only renamed_keys."""
        diagnostics.renamed_keys["old_prop"] = "new_prop"

    monkeypatch.setattr(coordinator, "_async_run_baseline_validation", _mock_baseline)

    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)
    # renamed_keys must be captured for candidate generation
    assert report.renamed_keys == {"old_prop": "new_prop"}
    # Must NOT add warning strings
    assert report.warnings == []
    # Must NOT determine severity
    assert report.severity is None


async def test_incompatible_repair_fork_fails_closed_without_precondition(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test fork repair fails closed when local file revision precondition cannot be captured."""
    issue_id = coordinator.get_incompatible_issue_id(
        "automation/fork_test.yaml", FunctionalDomain.AUTOMATION
    )
    issue_data: dict[str, object] = {
        "config_entry_id": coordinator.config_entry.entry_id,
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "path": "/config/blueprints/automation/fork_test.yaml",
        "relative_path": "automation/fork_test.yaml",
        "domain": FunctionalDomain.AUTOMATION.value,
        "name": "Fork Test",
        "source_url": "https://github.com/original/fork_test.yaml",
        "has_auto_fix": "false",
        "candidate_content": "",
        "diff_text": "",
        "breaks_in_ha_version": "2025.3.0",
        "severity": IncompatibilitySeverity.DEPRECATION.value,
        "warnings": "Deprecated service syntax",
    }
    flow = IncompatibleBlueprintRepairFlow(coordinator, issue_id, issue_data)

    monkeypatch.setattr(
        coordinator,
        "async_fetch_import_data",
        AsyncMock(
            return_value=(
                "blueprint:\n  name: Fork Content\n",
                "https://github.com/fork/fork.yaml",
                "author",
                "Fork",
                None,
            )
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=CompatibilityReport(severity=None)),
    )
    monkeypatch.setattr(
        BlueprintUpdateCoordinator,
        "_read_blueprint_file",
        MagicMock(return_value=("old_content", "old_hash")),
    )
    # Simulate capture_precondition returning missing precondition
    monkeypatch.setattr(
        BlueprintFileStore,
        "capture_precondition",
        MagicMock(return_value=FileRevisionPrecondition.missing()),
    )

    result = await flow.async_step_change_url({"url": "https://github.com/fork/fork.yaml"})
    # Must fail closed back to change_url with invalid_url error, not proceed to confirm_fork
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "change_url"
    assert (result.get("errors") or {}).get("url") == "invalid_url"

    # Also test _async_execute_fork_switch fails closed when precondition is missing
    flow._pending_url = "https://github.com/fork/fork.yaml"
    flow._pending_content = "blueprint:\n  name: Fork Content\n"
    flow._pending_precondition = None

    result_exec = await flow._async_execute_fork_switch()
    assert result_exec.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_exec.get("step_id") == "change_url"
    assert (result_exec.get("errors") or {}).get("url") == "invalid_url"


async def test_incompatible_repair_fork_auto_fix_diff_against_local_file(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test fork auto-fix step computes diff against local file content, not fork content."""
    issue_id = coordinator.get_incompatible_issue_id(
        "automation/local_diff_test.yaml", FunctionalDomain.AUTOMATION
    )
    issue_data: dict[str, object] = {
        "config_entry_id": coordinator.config_entry.entry_id,
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "path": "/config/blueprints/automation/local_diff_test.yaml",
        "relative_path": "automation/local_diff_test.yaml",
        "domain": FunctionalDomain.AUTOMATION.value,
        "name": "Local Diff Test",
        "source_url": "https://github.com/original/bp.yaml",
        "has_auto_fix": "false",
        "candidate_content": "",
        "diff_text": "",
        "severity": IncompatibilitySeverity.DEPRECATION.value,
    }
    flow = IncompatibleBlueprintRepairFlow(coordinator, issue_id, issue_data)
    flow._pending_url = "https://github.com/fork/bp.yaml"
    flow._pending_content = "blueprint:\n  name: Community Fork\n"
    modernized_content = "blueprint:\n  name: Modernized Candidate\n"
    flow._fork_candidate = (modernized_content, "- fork\n+ modernized")
    flow._pending_precondition = FileRevisionPrecondition.existing("local_hash_123")

    local_disk_content = "blueprint:\n  name: User Local Customized\n"
    monkeypatch.setattr(
        BlueprintUpdateCoordinator,
        "_read_blueprint_file",
        MagicMock(return_value=(local_disk_content, "local_hash_123")),
    )

    result = await flow.async_step_confirm_fork({"fork_action": RepairForkAction.AUTO_FIX.value})
    assert result.get("step_id") == "auto_fix"
    assert flow.candidate_content == modernized_content
    assert "User Local Customized" in flow.diff_text
    assert "Modernized Candidate" in flow.diff_text
    assert "- fork" not in flow.diff_text


async def test_manual_pin_preserved_during_update_checks(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that blueprints pinned with PinReason.MANUAL are never auto-unpinned."""
    rel_path = "automation/manually_pinned.yaml"
    full_path = f"/config/blueprints/{rel_path}"

    coordinator._persisted_metadata[rel_path] = {
        "pinned": True,
        "pinned_reason": PinReason.MANUAL.value,
        "remote_hash": "hash_v1",
        "source_url": "https://github.com/author/bp.yaml",
    }
    coordinator.data[full_path] = {
        "relative_path": rel_path,
        "pinned": True,
        "pinned_reason": PinReason.MANUAL.value,
        "local_hash": "hash_v1",
        "remote_hash": "hash_v2",
        "source_url": "https://github.com/author/bp.yaml",
        "domain": FunctionalDomain.AUTOMATION,
    }

    # 1. Source URL change must preserve manual pin
    info_new = {
        **coordinator.data[full_path],
        "source_url": "https://github.com/new_author/bp.yaml",
    }
    prev_dict = {
        **coordinator.data[full_path],
        "source_url": "https://github.com/author/bp.yaml",
    }
    coordinator._handle_source_url_change(full_path, info_new, prev_dict)
    assert coordinator._persisted_metadata[rel_path].get("pinned") is True
    assert coordinator._persisted_metadata[rel_path].get("pinned_reason") == PinReason.MANUAL.value
    assert coordinator._persisted_metadata[rel_path].get("remote_hash") is None
    assert (
        coordinator._persisted_metadata[rel_path].get("source_url")
        == "https://github.com/new_author/bp.yaml"
    )

    # 2. Ghost update detection must NOT clear pinned flag or make updatable
    info_ghost: dict[str, object] = {
        "local_hash": "hash_v1",
        "relative_path": rel_path,
        "pinned": True,
        "pinned_reason": PinReason.MANUAL.value,
    }
    prev_ghost: dict[str, object] = {
        "pinned": True,
        "pinned_reason": PinReason.MANUAL.value,
        "remote_hash": "hash_v2",
    }
    is_updatable, _, _, _ = coordinator._apply_ghost_update_detection(
        full_path, info_ghost, prev_ghost
    )
    assert is_updatable is False

    # 3. _process_blueprint_content must not auto-unpin manual pins
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=CompatibilityReport(severity=None)),
    )
    unpin_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_unpin_blueprint", unpin_mock)

    await coordinator._process_blueprint_content(
        full_path,
        coordinator.data[full_path],
        "blueprint:\n  name: V2\n",
        "https://github.com/author/bp.yaml",
        results_to_notify=[],
        updated_domains=set(),
    )
    unpin_mock.assert_not_called()
    assert coordinator._persisted_metadata[rel_path].get("pinned") is True


async def test_post_update_compatibility_guard_deletes_orphaned_issues(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that orphaned incompatible blueprint issues are purged when file is deleted."""
    monkeypatch.setattr(coordinator, "async_check_ha_version_update", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "async_save_ha_version", AsyncMock())

    all_blueprints = {
        "/config/blueprints/automation/existing.yaml": {
            "relative_path": "automation/existing.yaml",
            "content": "blueprint:\n  name: Existing\n",
            "domain": FunctionalDomain.AUTOMATION,
            "local_hash": "existing_hash",
        }
    }
    monkeypatch.setattr(
        coordinator, "async_scan_all_local_blueprint_files", AsyncMock(return_value=all_blueprints)
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=CompatibilityReport(severity=None)),
    )

    issue_registry = ir.async_get(hass)
    orphaned_issue_id = coordinator.get_incompatible_issue_id(
        "automation/deleted.yaml", FunctionalDomain.AUTOMATION
    )
    issue_registry.issues[(DOMAIN, orphaned_issue_id)] = MagicMock()

    delete_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)
    delete_mock.assert_any_call(hass, DOMAIN, orphaned_issue_id)


async def test_post_update_compatibility_guard_preserves_orphaned_issues_on_scan_failure(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that orphaned issues are NOT purged if blueprint validation fails."""
    monkeypatch.setattr(coordinator, "async_check_ha_version_update", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "async_save_ha_version", AsyncMock())

    all_blueprints = {
        "/config/blueprints/automation/broken.yaml": {
            "relative_path": "automation/broken.yaml",
            "content": "blueprint:\n  name: Broken\n",
            "domain": FunctionalDomain.AUTOMATION,
            "local_hash": "broken_hash",
        }
    }
    monkeypatch.setattr(
        coordinator, "async_scan_all_local_blueprint_files", AsyncMock(return_value=all_blueprints)
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(side_effect=RuntimeError("Validation crashed")),
    )

    issue_registry = ir.async_get(hass)
    orphaned_issue_id = coordinator.get_incompatible_issue_id(
        "automation/deleted.yaml", FunctionalDomain.AUTOMATION
    )
    issue_registry.issues[(DOMAIN, orphaned_issue_id)] = MagicMock()

    delete_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)
    delete_mock.assert_not_called()


async def test_post_update_compatibility_guard_handles_missing_relative_path(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test post-update guard handles blueprints with missing relative_path gracefully."""
    monkeypatch.setattr(coordinator, "async_check_ha_version_update", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "async_save_ha_version", AsyncMock())

    all_blueprints = {
        "/config/blueprints/automation/malformed.yaml": {
            "content": "blueprint:\n  name: Malformed\n",
            "domain": FunctionalDomain.AUTOMATION,
        },
        "/invalid/path/none.yaml": {
            "content": "",
        },
    }
    monkeypatch.setattr(
        coordinator, "async_scan_all_local_blueprint_files", AsyncMock(return_value=all_blueprints)
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=CompatibilityReport(severity=None)),
    )

    issue_registry = ir.async_get(hass)
    orphaned_issue_id = coordinator.get_incompatible_issue_id(
        "automation/deleted.yaml", FunctionalDomain.AUTOMATION
    )
    issue_registry.issues[(DOMAIN, orphaned_issue_id)] = MagicMock()

    delete_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)
    delete_mock.assert_any_call(hass, DOMAIN, orphaned_issue_id)


async def test_post_update_compatibility_guard_ignores_unrelated_prefix_issues(
    coordinator: BlueprintUpdateCoordinator, hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test post-update guard does not delete unrelated repair issues that share ID prefix."""
    monkeypatch.setattr(coordinator, "async_check_ha_version_update", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "async_save_ha_version", AsyncMock())

    all_blueprints = {
        "/config/blueprints/automation/valid.yaml": {
            "relative_path": "automation/valid.yaml",
            "content": "blueprint:\n  name: Valid\n",
            "domain": FunctionalDomain.AUTOMATION,
            "local_hash": "valid_hash",
        }
    }
    monkeypatch.setattr(
        coordinator, "async_scan_all_local_blueprint_files", AsyncMock(return_value=all_blueprints)
    )
    monkeypatch.setattr(
        coordinator,
        "async_validate_local_blueprint_compatibility",
        AsyncMock(return_value=CompatibilityReport(severity=None)),
    )

    issue_registry = ir.async_get(hass)
    unrelated_prefix_issue_id = "incompatible_blueprint_summary"
    unrelated_mock_1 = MagicMock()
    unrelated_mock_1.translation_key = "summary"
    issue_registry.issues[(DOMAIN, unrelated_prefix_issue_id)] = unrelated_mock_1

    unrelated_hash_issue_id = f"{RepairIssueType.INCOMPATIBLE_BLUEPRINT.value}_0123456789abcdef"
    unrelated_mock_2 = MagicMock()
    unrelated_mock_2.translation_key = "other_repair"
    unrelated_mock_2.data = {"issue_type": "other_repair"}
    issue_registry.issues[(DOMAIN, unrelated_hash_issue_id)] = unrelated_mock_2

    mismatched_issue_id = f"{RepairIssueType.INCOMPATIBLE_BLUEPRINT.value}_abcdef0123456789"
    unrelated_mock_3 = MagicMock()
    unrelated_mock_3.translation_key = RepairIssueType.INCOMPATIBLE_BLUEPRINT.value
    unrelated_mock_3.data = {
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "relative_path": "automation/different.yaml",
    }
    issue_registry.issues[(DOMAIN, mismatched_issue_id)] = unrelated_mock_3

    orphaned_issue_id = coordinator.get_incompatible_issue_id(
        "automation/deleted.yaml", FunctionalDomain.AUTOMATION
    )
    legit_orphaned_mock = MagicMock()
    legit_orphaned_mock.translation_key = RepairIssueType.INCOMPATIBLE_BLUEPRINT.value
    legit_orphaned_mock.data = {
        "issue_type": RepairIssueType.INCOMPATIBLE_BLUEPRINT.value,
        "relative_path": "automation/deleted.yaml",
        "domain": "automation",
    }
    issue_registry.issues[(DOMAIN, orphaned_issue_id)] = legit_orphaned_mock

    delete_mock = MagicMock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_mock)

    await coordinator.async_run_post_update_compatibility_guard(force=True)

    valid_issue_id = coordinator.get_incompatible_issue_id(
        "automation/valid.yaml", FunctionalDomain.AUTOMATION
    )
    # The valid blueprint has its issue cleared, and the orphaned issue is purged
    delete_mock.assert_any_call(hass, DOMAIN, valid_issue_id)
    delete_mock.assert_any_call(hass, DOMAIN, orphaned_issue_id)
    assert delete_mock.call_count == 2

    # None of the unrelated prefix issues should have been deleted
    deleted_issue_ids = [call.args[2] for call in delete_mock.call_args_list]
    assert unrelated_prefix_issue_id not in deleted_issue_ids
    assert unrelated_hash_issue_id not in deleted_issue_ids
    assert mismatched_issue_id not in deleted_issue_ids


async def test_modernize_legacy_blueprint_numeric_filter_defaults() -> None:
    """Test modernize_legacy_blueprint_yaml preserves existing numeric filter defaults."""
    legacy_content = """blueprint:
  name: Numeric Filters Test
  domain: automation
action:
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ states('sensor.temperature') | float > 20.0 }}"
          - condition: template
            value_template: "{{ states('sensor.humidity') | float(2) > 50.0 }}"
          - condition: template
            value_template: "{{ states('sensor.pressure') | float('1013.25') > 1000.0 }}"
        sequence:
          - action: notify.notify
            data:
              message: "{{ states('sensor.count') | int }}"
              extra: "{{ states('sensor.level') | int(3) }}"
              quoted: "{{ states('sensor.offset') | int('5') }}"
              templated: "{{ states('sensor.step') | int(default=1) }}"
"""
    modernized = modernize_legacy_blueprint_yaml(legacy_content, FunctionalDomain.AUTOMATION)
    # Filters without defaults are updated to (0)
    assert "| float(0)" in modernized
    assert "| int(0)" in modernized
    # Existing defaults are preserved without duplication
    assert "| float(2)" in modernized
    assert "| float('1013.25')" in modernized
    assert "| int(3)" in modernized
    assert "| int('5')" in modernized
    assert "| int(default=1)" in modernized
    assert "| float(2)(0)" not in modernized
    assert "| int(3)(0)" not in modernized


@pytest.mark.parametrize(
    "section",
    [
        "action:\n  - action: notify.notify\n    data:\n      message: |-\n        ",
        "trigger:\n  - trigger: template\n    value_template: |-\n      ",
    ],
    ids=["action", "trigger"],
)
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{ '| float and | int' }}", "{{ '| float and | int' }}"),
        ('{{ "| float and | int" }}', '{{ "| float and | int" }}'),
        ("{{ '| float' | float }}", "{{ '| float' | float(0) }}"),
        ('{{ "| int" | int }}', '{{ "| int" | int(0) }}'),
        (
            r"{{ 'it\'s | float' | int }}",
            r"{{ 'it\'s | float' | int(0) }}",
        ),
        (
            r'{{ "say \"| int\"" | float }}',
            r'{{ "say \"| int\"" | float(0) }}',
        ),
        (
            r"{{ 'path\\' | float | int }}",
            r"{{ 'path\\' | float(0) | int(0) }}",
        ),
        (
            "{{ (value | int) ~ '| float' ~ (other | float) }}",
            "{{ (value | int(0)) ~ '| float' ~ (other | float(0)) }}",
        ),
        (
            "{{ value | float(default='| int') | int(2) }}",
            "{{ value | float(default='| int') | int(2) }}",
        ),
        (
            "{% set value = '| float' %}{{ value | float }}",
            "{% set value = '| float' %}{{ value | float(0) }}",
        ),
        (
            '{% set value = "| int" | int %}{{ value }}',
            '{% set value = "| int" | int(0) %}{{ value }}',
        ),
        (
            "{{ '| float }}' | int }}",
            "{{ '| float }}' | int(0) }}",
        ),
        (
            '{% set value = "| int %}" | float %}{{ value }}',
            '{% set value = "| int %}" | float(0) %}{{ value }}',
        ),
    ],
)
def test_modernize_numeric_filters_preserves_jinja_strings(
    section: str, template: str, expected: str
) -> None:
    """Preserve Jinja string literals while modernizing action and trigger filters."""
    prefix = "blueprint:\n  name: Quoted Numeric Filters\n  domain: automation\n" + section
    content = prefix + template + "\n"
    expected_content = prefix + expected + "\n"

    modernized = modernize_legacy_blueprint_yaml(content, FunctionalDomain.AUTOMATION)

    assert modernized == expected_content
    assert modernize_legacy_blueprint_yaml(modernized, FunctionalDomain.AUTOMATION) == modernized


async def test_wrap_action_target_blocks_sequence_no_dash() -> None:
    """Test target wrapping when action property line starts after a dash on sequence item."""
    legacy_content = """blueprint:
  name: Sequence Formatting Test
  domain: automation
sequence:
  - action: light.turn_on
    entity_id: light.bulb
"""
    modernized = modernize_legacy_blueprint_yaml(legacy_content, FunctionalDomain.AUTOMATION)
    assert "target:" in modernized
    assert "entity_id: light.bulb" in modernized


@pytest.mark.parametrize(
    "guard_error", [None, RuntimeError("guard failed"), asyncio.CancelledError()]
)
async def test_check_compatibility_service(
    coordinator: BlueprintUpdateCoordinator, hass, guard_error, caplog
) -> None:
    """Test forced checks isolate coordinator errors while propagating cancellation."""
    entry = coordinator.config_entry
    coordinator_mock = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator_mock.async_setup = AsyncMock()
    coordinator_mock.async_config_entry_first_refresh = AsyncMock()
    coordinator_mock.async_schedule_post_update_compatibility_guard = MagicMock()
    coordinator_mock.async_run_post_update_compatibility_guard = AsyncMock(side_effect=guard_error)
    coordinator_mock.config_entry = entry
    coordinator_mock.data = {}

    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    with (
        patch(
            "custom_components.blueprints_updater.BlueprintUpdateCoordinator",
            return_value=coordinator_mock,
        ),
        patch("custom_components.blueprints_updater.async_register_admin_service") as mock_register,
        patch.object(hass.services, "has_service", return_value=False),
    ):
        assert await async_setup_entry(hass, entry) is True

    check_call = next(
        call
        for call in mock_register.call_args_list
        if (len(call.args) > 2 and call.args[2] == IntegrationService.CHECK_COMPATIBILITY)
        or call.kwargs.get("service") == IntegrationService.CHECK_COMPATIBILITY
    )
    handler = check_call.args[3] if len(check_call.args) > 3 else check_call.kwargs["handler"]
    next_coordinator = MagicMock(spec=BlueprintUpdateCoordinator)
    next_guard = next_coordinator.async_run_post_update_compatibility_guard = AsyncMock()
    hass.data[DOMAIN]["coordinators"]["next_entry"] = next_coordinator
    if isinstance(guard_error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await handler(MagicMock())
        next_guard.assert_not_awaited()
        assert "Error checking blueprint compatibility" not in caplog.text
    else:
        await handler(MagicMock())
        next_guard.assert_awaited_once_with(force=True)
        if guard_error is not None:
            assert (
                f"Error checking blueprint compatibility for entry {entry.entry_id}" in caplog.text
            )
            assert "guard failed" in caplog.text
    coordinator_mock.async_run_post_update_compatibility_guard.assert_awaited_once_with(force=True)


async def test_wrap_action_target_blocks_nested_list_items() -> None:
    """Test target wrapping does not truncate nested list items such as multiple entities."""
    legacy_content = """blueprint:
  name: Nested Lists Test
  domain: automation
action:
  - service: light.turn_on
    entity_id:
      - light.kitchen
      - light.living_room
    data:
      brightness: 100
"""
    modernized = modernize_legacy_blueprint_yaml(legacy_content, FunctionalDomain.AUTOMATION)
    assert "target:" in modernized
    assert "entity_id:" in modernized
    assert "- light.kitchen" in modernized
    assert "- light.living_room" in modernized
    assert "brightness: 100" in modernized


def test_get_ha_version_detection(hass: MagicMock) -> None:
    """Test get_ha_version returns configured version or core constant."""
    hass.config.version = "2025.1.0"
    assert get_ha_version(hass) == "2025.1.0"

    del hass.config.version
    assert get_ha_version(hass) == __version__
    assert get_ha_version(None) == __version__


def test_is_dummy_validation_error_matching() -> None:
    """Test is_dummy_validation_error detects dummy devices, entities, areas, and labels."""
    # Standard dummy identifiers when synthetic_values is None
    for err in (
        HomeAssistantError("Unknown device 'dummy_device_id'"),
        vol.Invalid("Unknown entity 'test.dummy'"),
        HomeAssistantError("Unknown area 'dummy_area_id'"),
        HomeAssistantError("Unknown floor 'dummy_floor_id'"),
        HomeAssistantError("Unknown label 'dummy_label_id'"),
        HomeAssistantError("Device not found: dummy_device_id"),
        HomeAssistantError("Device not found: dummy_device_id."),
        HomeAssistantError("Device dummy_device_id not found."),
        HomeAssistantError("Could not find device dummy_device_id-"),
        vol.Invalid("Entity not found: test.dummy"),
        vol.Invalid("Entity not found: test.dummy."),
        vol.Invalid("Entity test.dummy does not exist."),
        vol.Invalid("Entity not found: test.dummy..."),
    ):
        assert is_dummy_validation_error(err)

    # Errors where blueprint author uses 'dummy' string must NOT match
    for err in (
        vol.Invalid("extra keys not allowed @ data['dummy']"),
        vol.Invalid("extra keys not allowed @ data['dummy_key']"),
        vol.Invalid("Invalid action key 'dummy_action'"),
        HomeAssistantError("Service notify.dummy not found"),
        HomeAssistantError("Template error: 'dummy' is undefined"),
        HomeAssistantError("Unknown entity 'light.dummy_author_light'"),
        HomeAssistantError("Service light.turn_on not found"),
        vol.Invalid("Invalid action key"),
        vol.Invalid("extra keys not allowed @ data['custom_prop']"),
    ):
        assert not is_dummy_validation_error(err)

    # Contextual matching with injected synthetic_values
    synthetic = {"dummy_device_id", "person.dummy"}
    assert is_dummy_validation_error(
        HomeAssistantError("Unknown device 'dummy_device_id'"), synthetic
    )
    assert is_dummy_validation_error(vol.Invalid("Unknown entity 'person.dummy'"), synthetic)
    assert not is_dummy_validation_error(
        HomeAssistantError("Unknown device 'real_device_id'"), synthetic
    )
    assert not is_dummy_validation_error(vol.Invalid("Unknown entity 'light.dummy'"), synthetic)
    # Device automation exceptions without identifier must NOT be classified as dummy
    # merely because a synthetic value contains 'device'
    assert not is_dummy_validation_error(
        InvalidDeviceAutomationConfig("Unable to resolve webhook ID from the device ID"),
        synthetic,
    )
    assert not is_dummy_validation_error(
        InvalidDeviceAutomationConfig("Unable to resolve webhook ID from the device ID"),
        {"person.dummy"},
    )

    # When error message or attributes reference a synthetic device ID, classify as dummy
    assert is_dummy_validation_error(
        InvalidDeviceAutomationConfig("Device dummy_device_id not found"),
        synthetic,
    )

    class _MockDeviceAutomationException(InvalidDeviceAutomationConfig):
        """Mock device automation exception with structured attributes."""

        device_id: str
        path: list[str | int]

    exc_with_attr = _MockDeviceAutomationException(
        "Unable to resolve webhook ID from the device ID"
    )
    exc_with_attr.device_id = "dummy_device_id"
    assert is_dummy_validation_error(exc_with_attr, synthetic)

    exc_with_tp = InvalidDeviceAutomationConfig(
        "Device error",
        translation_placeholders={"device_id": "dummy_device_id"},
    )
    assert is_dummy_validation_error(exc_with_tp, synthetic)

    # When exception has no identifier, accept only when failing config path
    # resolves to synthetic input
    exc_with_path = _MockDeviceAutomationException(
        "Unable to resolve webhook ID from the device ID"
    )
    exc_with_path.path = ["trigger", 0, "device_id"]
    cfg_with_dummy = {"trigger": [{"device_id": "dummy_device_id"}]}
    assert is_dummy_validation_error(exc_with_path, synthetic, substituted_config=cfg_with_dummy)

    cfg_with_author = {"trigger": [{"device_id": "author_device_123"}]}
    assert not is_dummy_validation_error(
        exc_with_path, synthetic, substituted_config=cfg_with_author
    )
    assert is_dummy_validation_error(EntityNotFound("Unknown entity 'test.dummy'"))
    assert is_dummy_validation_error(EntityNotFound("Unknown entity 'person.dummy'"), synthetic)
    assert not is_dummy_validation_error(
        EntityNotFound("Unknown entity 'light.real_entity'"), synthetic
    )

    # Author uses dummy in extra keys or action with synthetic app input generating 'dummy'
    synthetic_app = {"dummy"}
    assert not is_dummy_validation_error(
        vol.Invalid("extra keys not allowed @ data['dummy_extra_key']"), synthetic_app
    )
    assert not is_dummy_validation_error(
        vol.Invalid("extra keys not allowed @ data['dummy']"), synthetic_app
    )
    assert not is_dummy_validation_error(
        vol.Invalid("Invalid action key 'dummy_action'"), synthetic_app
    )
    assert not is_dummy_validation_error(
        HomeAssistantError("Service notify.dummy not found"), synthetic_app
    )
    assert not is_dummy_validation_error(
        TemplateError("Template error: 'dummy' is undefined"), synthetic_app
    )
    assert not is_dummy_validation_error(
        HomeAssistantError("Unknown entity 'light.dummy_author_light'"), synthetic_app
    )
    assert is_dummy_validation_error(vol.Invalid("Unknown app 'dummy'"), synthetic_app)
    assert is_dummy_validation_error(vol.Invalid("App not found: dummy"), synthetic_app)


def test_error_identifies_synthetic_id_strips_trailing_punctuation() -> None:
    """Test _error_identifies_synthetic_id strips trailing periods and hyphens."""
    from custom_components.blueprints_updater.blueprint_validation import (
        _error_identifies_synthetic_id,
    )

    assert _error_identifies_synthetic_id("Device dummy_device_id not found.", "dummy_device_id")
    assert _error_identifies_synthetic_id("Device not found: dummy_device_id.", "dummy_device_id")
    assert _error_identifies_synthetic_id("Device not found: dummy_device_id-", "dummy_device_id")
    assert _error_identifies_synthetic_id("Device not found: dummy_device_id...", "dummy_device_id")
    assert _error_identifies_synthetic_id("Entity light.dummy does not exist.", "light.dummy")
    assert _error_identifies_synthetic_id("Unknown entity: light.dummy.", "light.dummy")
    assert not _error_identifies_synthetic_id("Unknown entity: light.dummy_other.", "light.dummy")
    assert not _error_identifies_synthetic_id("Service notify.dummy not found.", "dummy")


def test_standard_dummy_ids_drift_prevention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test standard dummy IDs are dynamically derived to avoid drift."""
    from custom_components.blueprints_updater.blueprint_validation import (
        _ID_DUMMY_DEFAULTS,
        STANDARD_DUMMY_IDS,
        get_standard_dummy_ids,
    )

    # Verify standard dummy IDs contain all current _ID_DUMMY_DEFAULTS values
    standard_ids = get_standard_dummy_ids()
    assert isinstance(standard_ids, frozenset)
    assert standard_ids == STANDARD_DUMMY_IDS
    for expected_id in _ID_DUMMY_DEFAULTS.values():
        assert expected_id in standard_ids

    assert "test.dummy" in standard_ids
    assert "sensor.dummy" in standard_ids

    # Test dynamic reflection when _ID_DUMMY_DEFAULTS is updated
    monkeypatch.setitem(_ID_DUMMY_DEFAULTS, "custom_zone", "dummy_custom_zone_id")
    fresh_ids = get_standard_dummy_ids()
    assert "dummy_custom_zone_id" in fresh_ids
    assert is_dummy_validation_error(HomeAssistantError("Unknown zone 'dummy_custom_zone_id'"))


async def test_baseline_validation_ignores_dummy_device_error(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test baseline validation ignores dummy device failures for unused blueprints."""
    rel_path = "automation/homeassistant/notify_leaving_zone.yaml"
    path = "/config/blueprints/automation/homeassistant/notify_leaving_zone.yaml"
    content = (
        "blueprint:\n"
        "  name: Zone Notification\n"
        "  domain: automation\n"
        "  input:\n"
        "    notify_device:\n"
        "      selector:\n"
        "        device:\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: sensor.test\n"
        "actions:\n"
        "  - domain: mobile_app\n"
        "    type: notify\n"
        "    device_id: !input notify_device\n"
        "    message: test\n"
    )

    async def _mock_run_domain_validator(*args: object, **kwargs: object) -> None:
        """Simulate device automation validation failure on unknown dummy device."""
        raise HomeAssistantError("Unknown device 'dummy_device_id'")

    monkeypatch.setattr(coordinator, "_async_run_domain_validator", _mock_run_domain_validator)
    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)

    # Must NOT report breaking failure or repair issue for missing dummy device
    assert report.severity is None
    assert report.errors == []


async def test_baseline_validation_ignores_dummy_entity_error(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test baseline validation ignores dummy entity failures for unused blueprints."""
    rel_path = "automation/homeassistant/zone_notify.yaml"
    path = "/config/blueprints/automation/homeassistant/zone_notify.yaml"
    content = (
        "blueprint:\n"
        "  name: Zone Entity Test\n"
        "  domain: automation\n"
        "  input:\n"
        "    person_entity:\n"
        "      selector:\n"
        "        entity:\n"
        "          filter:\n"
        "            domain: person\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: sensor.test\n"
        "actions:\n"
        "  - action: notify.notify\n"
        "    target:\n"
        "      entity_id: !input person_entity\n"
    )

    async def _mock_run_domain_validator(*args: object, **kwargs: object) -> None:
        """Simulate entity validation failure on unknown dummy entity."""
        raise vol.Invalid("Unknown entity 'person.dummy'")

    monkeypatch.setattr(coordinator, "_async_run_domain_validator", _mock_run_domain_validator)
    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)

    assert report.severity is None
    assert report.errors == []


async def test_baseline_validation_reports_error_when_author_uses_dummy_string(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test baseline validation does not suppress real errors containing 'dummy' string."""
    rel_path = "automation/homeassistant/author_dummy_test.yaml"
    path = "/config/blueprints/automation/homeassistant/author_dummy_test.yaml"
    content = (
        "blueprint:\n"
        "  name: Author Dummy Test\n"
        "  domain: automation\n"
        "  input:\n"
        "    notify_device:\n"
        "      selector:\n"
        "        device:\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: sensor.test\n"
        "actions:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.living_room\n"
        "    dummy_extra_key: true\n"
    )

    async def _mock_run_domain_validator(*args: object, **kwargs: object) -> None:
        """Simulate schema validation error on author's dummy property."""
        raise vol.Invalid("extra keys not allowed @ data['dummy_extra_key']")

    monkeypatch.setattr(coordinator, "_async_run_domain_validator", _mock_run_domain_validator)
    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)

    assert report.severity == IncompatibilitySeverity.BREAKING
    assert any("dummy_extra_key" in err for err in report.errors)


def test_diff_structural_configs_variables_not_deprecated() -> None:
    """Test diff_structural_configs does not flag popped variables as deprecated."""
    input_cfg = {
        CONF_VARIABLES: {
            "reference_entity": "binary_sensor.test",
        },
        "binary_sensor": {
            "state": "{{ states(reference_entity) }}",
        },
    }
    validated_cfg = {
        "binary_sensor": [
            {
                "state": "{{ states(reference_entity) }}",
                CONF_VARIABLES: {
                    "reference_entity": "binary_sensor.test",
                },
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert CONF_VARIABLES not in diagnostics.deprecated_keys
    assert diagnostics.deprecated_keys == []


def test_diff_structural_configs_deprecated_keys_detected() -> None:
    """Test diff_structural_configs detects genuinely removed keys and handles list reshaping."""
    input_cfg = {
        "legacy_root_key": "old_value",
        "binary_sensor": {
            "name": "Test Sensor",
            "deprecated_inner_key": "some_value",
        },
    }
    validated_cfg = {
        "binary_sensor": [
            {
                "name": "Test Sensor",
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert "legacy_root_key" in diagnostics.deprecated_keys
    assert "deprecated_inner_key" in diagnostics.deprecated_keys
    assert not diagnostics.renamed_keys


def test_diff_structural_configs_unrelated_variables_still_deprecated() -> None:
    """Test root variables are deprecated when child variables differ."""
    input_cfg = {
        CONF_VARIABLES: {
            "deprecated_root_var": "legacy_val",
        },
        "binary_sensor": {
            "state": "on",
        },
    }
    validated_cfg = {
        "binary_sensor": [
            {
                "state": "on",
                CONF_VARIABLES: {
                    "unrelated_child_var": "other_val",
                },
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert CONF_VARIABLES in diagnostics.deprecated_keys


def test_stringify_keys_serialization() -> None:
    """Test stringify_keys converts non-string dictionary keys for orjson."""
    data: dict[object, object] = {
        100: "numeric_key",
        ("nested", "tuple"): {
            200: "inner_val",
            "list": [{300: "item_val"}],
        },
    }
    stringified = stringify_keys(data)
    encoded = orjson.dumps(stringified)
    decoded = orjson.loads(encoded)

    assert decoded["100"] == "numeric_key"
    assert decoded["('nested', 'tuple')"]["200"] == "inner_val"
    assert decoded["('nested', 'tuple')"]["list"][0]["300"] == "item_val"


def test_stringify_keys_collision_handling() -> None:
    """Test stringify_keys detects collisions between distinct source keys."""
    colliding_data: dict[object, object] = {
        1: "int_key",
        "1": "str_key",
    }
    with pytest.raises(ValueError, match="Key collision detected in stringify_keys"):
        stringify_keys(colliding_data)

    preserved = stringify_keys(colliding_data, preserve_collisions=True)
    assert isinstance(preserved, dict)
    assert preserved.get("1") == "int_key"
    assert preserved.get("1_str") == "str_key"


def test_diff_structural_configs_key_collision_rejected() -> None:
    """Test diff_structural_configs detects and rejects colliding keys before storing renames."""
    input_cfg: dict[object, object] = {
        1: "service_val",
        "1": "other_val",
    }
    validated_cfg = {
        "action": "service_val",
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    # Collision is rejected rather than silently storing a corrupted rename
    assert "1" not in diagnostics.renamed_keys
    assert not diagnostics.renamed_keys


def test_diff_structural_configs_conflicting_rename_rejected() -> None:
    """Test diff_structural_configs does not overwrite existing rename diagnostics on conflict."""
    diagnostics = ValidationDiagnostics()
    diagnostics.renamed_keys["service"] = "action"

    input_cfg = {
        "service": "turn_on",
    }
    validated_cfg = {
        "perform_action": "turn_on",
    }
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    # Existing rename "service" -> "action" is preserved and not overwritten by "perform_action"
    assert diagnostics.renamed_keys["service"] == "action"


async def test_baseline_validation_reports_error_when_app_input_and_author_dummy_extra_key(
    coordinator: BlueprintUpdateCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test baseline validation preserves extra-key error when app input generates dummy."""
    from homeassistant.helpers import selector as ha_selector

    if "app" not in getattr(ha_selector, "SELECTORS", {}):

        class _MockAppSelector(ha_selector.Selector):
            """Mock app selector for Home Assistant versions prior to core app selector."""

            CONFIG_SCHEMA = vol.Schema({})

            def __init__(self, config: object = None) -> None:
                """Init mock app selector."""

        if hasattr(ha_selector, "SELECTORS") and isinstance(ha_selector.SELECTORS, dict):
            monkeypatch.setitem(ha_selector.SELECTORS, "app", _MockAppSelector)

    rel_path = "automation/homeassistant/app_dummy_test.yaml"
    path = "/config/blueprints/automation/homeassistant/app_dummy_test.yaml"
    content = (
        "blueprint:\n"
        "  name: App Dummy Test\n"
        "  domain: automation\n"
        "  input:\n"
        "    app_target:\n"
        "      selector:\n"
        "        app: {}\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: sensor.test\n"
        "actions:\n"
        "  - action: media_player.play_media\n"
        "    target:\n"
        "      entity_id: media_player.living_room\n"
        "    dummy_extra_key: true\n"
    )

    async def _mock_run_domain_validator(*args: object, **kwargs: object) -> None:
        """Simulate schema validation error on author's dummy property."""
        raise vol.Invalid("extra keys not allowed @ data['dummy_extra_key']")

    monkeypatch.setattr(coordinator, "_async_run_domain_validator", _mock_run_domain_validator)
    report = await coordinator.async_validate_local_blueprint_compatibility(rel_path, path, content)

    assert report.severity == IncompatibilitySeverity.BREAKING
    assert any("dummy_extra_key" in err for err in report.errors)


def test_is_relocated_value_none_keys_and_missing_keys() -> None:
    """Test _is_relocated_value requires keys to exist in target even when value is None."""
    from custom_components.blueprints_updater.coordinator import _is_relocated_value

    assert _is_relocated_value({"a": None}, {"a": None})
    assert not _is_relocated_value({"a": None}, {})
    assert not _is_relocated_value({"a": None}, {"b": None})
    assert _is_relocated_value({"a": None, "b": 1}, {"a": None, "b": 1, "c": 2})
    assert not _is_relocated_value({"a": None, "b": 1}, {"b": 1})


def test_diff_structural_configs_variables_not_suppressed_when_already_in_child() -> None:
    """Test root variables are deprecated if the same value already existed in the child."""
    input_cfg = {
        CONF_VARIABLES: {
            "reference_entity": "binary_sensor.test",
        },
        "binary_sensor": {
            "state": "{{ states(reference_entity) }}",
            CONF_VARIABLES: {
                "reference_entity": "binary_sensor.test",
            },
        },
    }
    validated_cfg = {
        "binary_sensor": [
            {
                "state": "{{ states(reference_entity) }}",
                CONF_VARIABLES: {
                    "reference_entity": "binary_sensor.test",
                },
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert CONF_VARIABLES in diagnostics.deprecated_keys


def test_diff_nested_values_wrapper_dict_triggers() -> None:
    """Test _diff_nested_values unwraps dict with single triggers list and uses triggers path."""
    input_cfg = {
        "triggers": {
            "triggers": [
                {
                    "platform": "state",
                    "entity_id": "light.first",
                    "service": "light.turn_on",
                },
                {
                    "platform": "state",
                    "entity_id": "light.second",
                    "service": "light.turn_off",
                },
            ]
        }
    }
    validated_cfg = {
        "triggers": [
            {
                "platform": "state",
                "entity_id": "light.first",
                "action": "light.turn_on",
            },
            {
                "platform": "state",
                "entity_id": "light.second",
                "action": "light.turn_off",
            },
        ]
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    # Renamed key "service" -> "action" is detected across items in the unwrapped triggers list
    assert diagnostics.renamed_keys.get("service") == "action"


def test_extract_synthetic_dummy_values_retains_metadata_and_suppresses_ordinary_errors() -> None:
    """Test dummy values retain input/path metadata and suppress appropriately."""
    bp_dict: dict[str, object] = {
        "blueprint": {
            "name": "Metadata Test",
            "domain": "automation",
            "input": {
                "state_inp": {
                    "name": "State Input",
                    "selector": {"state": {"entity_id": "light.dummy"}},
                },
                "device_inp": {
                    "name": "Device Input",
                    "selector": {"device": {}},
                },
            },
        },
        "trigger": [
            {
                "platform": "state",
                "entity_id": "light.dummy",
                "state": Input("state_inp"),
            }
        ],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"device_id": Input("device_inp")},
            }
        ],
    }

    synthetic = extract_synthetic_dummy_values(bp_dict)
    assert isinstance(synthetic, SyntheticDummyValues)

    # Synthetic identifiers (e.g. dummy_device_id) are in the free-standing token set
    assert "dummy_device_id" in synthetic
    # Ordinary string values (e.g. "on") are NOT in the free-standing token set
    assert "on" not in synthetic

    # Verify per-input metadata retention
    state_entry = synthetic.entries_by_input["state_inp"]
    assert state_entry.input_name == "state_inp"
    assert ("trigger", 0, "state") in state_entry.paths
    assert "on" in state_entry.ordinary_strings
    assert not state_entry.synthetic_ids

    device_entry = synthetic.entries_by_input["device_inp"]
    assert "dummy_device_id" in device_entry.synthetic_ids

    # 1. Free-standing error with ordinary string 'on' is NOT suppressed without matching path
    assert not is_dummy_validation_error(
        HomeAssistantError("Invalid state 'on'"),
        synthetic,
    )
    assert not is_dummy_validation_error(
        vol.Invalid("Invalid state 'on'"),
        synthetic,
    )

    # 2. Error at path matching the input's substituted path IS suppressed
    exc_matching = vol.Invalid("Invalid state 'on'", path=["trigger", 0, "state"])
    substituted_config = {
        "trigger": [
            {
                "platform": "state",
                "entity_id": "light.dummy",
                "state": "on",
            }
        ]
    }
    assert is_dummy_validation_error(
        exc_matching,
        synthetic,
        substituted_config=substituted_config,
    )

    # 3. Error at path NOT matching the input (e.g. another trigger) is NOT suppressed
    exc_author_error = vol.Invalid("Invalid state 'on'", path=["trigger", 1, "state"])
    assert not is_dummy_validation_error(
        exc_author_error,
        synthetic,
        substituted_config=substituted_config,
    )

    # 4. Extra-keys error at matching input path is NOT suppressed
    exc_extra_keys = vol.Invalid(
        "extra keys not allowed @ data['trigger'][0]['state']",
        path=["trigger", 0, "state"],
    )
    assert not is_dummy_validation_error(
        exc_extra_keys,
        synthetic,
        substituted_config=substituted_config,
    )

    # 5. MultipleInvalid with synthetic error and author error is NOT suppressed
    mult_with_author = vol.MultipleInvalid([exc_matching, exc_author_error])
    assert not is_dummy_validation_error(
        mult_with_author,
        synthetic,
        substituted_config=substituted_config,
    )

    # 6. MultipleInvalid where all errors are synthetic IS suppressed
    mult_all_synthetic = vol.MultipleInvalid([exc_matching])
    assert is_dummy_validation_error(
        mult_all_synthetic,
        synthetic,
        substituted_config=substituted_config,
    )

    # 7. Empty MultipleInvalid is NOT suppressed
    assert not is_dummy_validation_error(
        vol.MultipleInvalid([]),
        synthetic,
        substituted_config=substituted_config,
    )


def test_blueprint_select_option_extra_does_not_waive_extra_keys_error() -> None:
    """Test select option 'extra' cannot waive 'extra keys not allowed' errors."""
    bp_dict: dict[str, object] = {
        "blueprint": {
            "name": "Exploit Attempt",
            "domain": "automation",
            "input": {
                "select_input": {
                    "name": "Select Input",
                    "selector": {
                        "select": {
                            "options": ["extra", "normal"],
                        }
                    },
                },
            },
        },
        "trigger": [
            {
                "platform": "state",
                "entity_id": "light.test",
            }
        ],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.test"},
                "data": {"mode": Input("select_input")},
            }
        ],
    }

    synthetic = extract_synthetic_dummy_values(bp_dict)

    # "extra" is a blueprint-controlled ordinary value and must NOT be in the synthetic tokens set
    assert "extra" not in synthetic
    assert "extra" in synthetic.entries_by_input["select_input"].ordinary_strings

    # An unrelated schema validation failure (e.g. author wrote invalid extra keys in action)
    unrelated_err = vol.Invalid(
        "extra keys not allowed @ data['action'][0]['unrelated_bad_key']",
        path=["action", 0, "unrelated_bad_key"],
    )
    substituted_config = {
        "trigger": [{"platform": "state", "entity_id": "light.test"}],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.test"},
                "data": {"mode": "extra"},
                "unrelated_bad_key": "bad_value",
            }
        ],
    }

    # The unrelated error must NOT be classified as a dummy validation error
    assert not is_dummy_validation_error(
        unrelated_err,
        synthetic,
        substituted_config=substituted_config,
    )


def test_is_synthetic_identifier_restricts_to_generated_shapes() -> None:
    """Test is_synthetic_identifier matches explicit shapes and keeps author tokens ordinary."""
    # Standard and generated dummy shapes
    assert is_synthetic_identifier("dummy_device_id")
    assert is_synthetic_identifier("dummy_area_id")
    assert is_synthetic_identifier("test.dummy")
    assert is_synthetic_identifier("sensor.dummy")
    assert is_synthetic_identifier("dummy")

    # Author-defined values containing 'dummy' without explicit shapes
    assert not is_synthetic_identifier("my_dummy_mode")
    assert not is_synthetic_identifier("custom_dummy")
    assert not is_synthetic_identifier("is_dummy")
    assert not is_synthetic_identifier("dummy123")

    # Blueprint using select option 'my_dummy_mode'
    bp_dict: dict[str, object] = {
        "blueprint": {
            "name": "Select Dummy Mode Test",
            "domain": "automation",
            "input": {
                "mode_inp": {
                    "name": "Mode Input",
                    "selector": {
                        "select": {
                            "options": ["my_dummy_mode", "standard_mode"],
                        }
                    },
                },
            },
        },
        "trigger": [{"platform": "state", "entity_id": "light.test"}],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.test"},
                "data": {"mode": Input("mode_inp")},
            }
        ],
    }

    synthetic = extract_synthetic_dummy_values(bp_dict)
    assert "my_dummy_mode" not in synthetic
    assert "my_dummy_mode" in synthetic.entries_by_input["mode_inp"].ordinary_strings

    # A generic baseline error mentioning my_dummy_mode without path is not suppressed
    unrelated_err = HomeAssistantError("Option my_dummy_mode failed during setup")
    assert not is_dummy_validation_error(unrelated_err, synthetic)


def test_maps_failing_path_rejects_unresolved_path_and_preserves_aliased_path() -> None:
    """Test failing path rejection on traversal failure and preservation of aliased paths."""
    # Test _resolve_config_path handles singular/plural aliases
    cfg_plural = {"actions": [{"target": {"device_id": "dummy_device_id"}}]}
    assert (
        _resolve_config_path(cfg_plural, ["action", 0, "target", "device_id"]) == "dummy_device_id"
    )

    cfg_singular = {"action": [{"target": {"device_id": "dummy_device_id"}}]}
    assert (
        _resolve_config_path(cfg_singular, ["actions", 0, "target", "device_id"])
        == "dummy_device_id"
    )

    cfg_trigger = {"triggers": [{"platform": "state", "entity_id": "test.dummy"}]}
    assert _resolve_config_path(cfg_trigger, ["trigger", 0, "entity_id"]) == "test.dummy"

    # Path traversal failure returns None
    assert _resolve_config_path(cfg_plural, ["action", 0, "target", "nonexistent"]) is None

    # Blueprint defining device input under plural actions
    bp_dict: dict[str, object] = {
        "blueprint": {
            "name": "Aliased Device Path Test",
            "domain": "automation",
            "input": {
                "dev_inp": {
                    "name": "Target Device",
                    "selector": {"device": {}},
                },
            },
        },
        "actions": [
            {
                "service": "light.turn_on",
                "target": {"device_id": Input("dev_inp")},
            }
        ],
    }
    synthetic = extract_synthetic_dummy_values(bp_dict)
    assert isinstance(synthetic, SyntheticDummyValues)

    # 1. Aliased path (HA reports 'action', config has 'actions') resolves and is suppressed
    class _MockDeviceError(InvalidDeviceAutomationConfig):
        """Mock device automation exception."""

        path: list[str | int]

    aliased_err = _MockDeviceError("Unable to resolve webhook ID from the device ID")
    aliased_err.path = ["action", 0, "target", "device_id"]
    substituted_config = {
        "actions": [
            {
                "service": "light.turn_on",
                "target": {"device_id": "dummy_device_id"},
            }
        ]
    }
    assert is_dummy_validation_error(
        aliased_err,
        synthetic,
        substituted_config=substituted_config,
    )

    # 2. Unresolved sibling path under input prefix must NOT be matched or suppressed
    sibling_err = vol.Invalid("Invalid sibling", path=["action", 0, "target", "bad_sibling"])
    assert not is_dummy_validation_error(
        sibling_err,
        synthetic,
        substituted_config=substituted_config,
    )

    # 3. Direct call with resolved_val=None must not match
    assert not synthetic.maps_failing_path_to_input(
        ["action", 0, "target", "device_id"],
        resolved_val=None,
    )


def test_diff_structural_configs_variables_script_variables_not_deprecated() -> None:
    """Test diff_structural_configs does not flag variables when wrapped in ScriptVariables."""
    from homeassistant.helpers.script_variables import ScriptVariables

    input_cfg = {
        CONF_VARIABLES: {
            "reference_entity": "binary_sensor.test",
        },
        "binary_sensor": {
            "state": "{{ states(reference_entity) }}",
        },
    }
    validated_cfg = {
        "binary_sensor": [
            {
                "state": "{{ states(reference_entity) }}",
                CONF_VARIABLES: ScriptVariables(
                    {
                        "reference_entity": "binary_sensor.test",
                    }
                ),
            }
        ],
    }
    diagnostics = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diagnostics)

    assert CONF_VARIABLES not in diagnostics.deprecated_keys
    assert diagnostics.deprecated_keys == []


def test_diff_structural_configs_script_variables_automation_root() -> None:
    """Test diff_structural_configs handles root ScriptVariables in automation/script domains."""
    from homeassistant.helpers.script_variables import ScriptVariables

    # 1. Valid automation with root variables wrapped in ScriptVariables: no deprecations
    input_cfg = {
        CONF_VARIABLES: {"my_var": "value"},
        "trigger": [{"platform": "state"}],
    }
    validated_cfg = {
        CONF_VARIABLES: ScriptVariables({"my_var": "value"}),
        "trigger": [{"platform": "state"}],
    }
    diag = ValidationDiagnostics()
    diff_structural_configs(input_cfg, validated_cfg, diag)
    assert diag.deprecated_keys == []

    # 2. Inner variable dropped inside ScriptVariables is correctly detected
    input_cfg_dropped = {
        CONF_VARIABLES: {"legacy_var": "value"},
        "trigger": [{"platform": "state"}],
    }
    validated_cfg_dropped = {
        CONF_VARIABLES: ScriptVariables({}),
        "trigger": [{"platform": "state"}],
    }
    diag_dropped = ValidationDiagnostics()
    diff_structural_configs(input_cfg_dropped, validated_cfg_dropped, diag_dropped)
    assert diag_dropped.deprecated_keys == ["legacy_var"]
