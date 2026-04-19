"""Tests for Blueprints Updater Advanced Compatibility Guard logic."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.blueprints_updater.const import DOMAIN, RISK_TYPE_TRANSLATIONS
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


# ---------------------------------------------------------------------------
# RISK_TYPE_TRANSLATIONS (const.py)
# ---------------------------------------------------------------------------


class TestRiskTypeTranslations:
    """Tests for RISK_TYPE_TRANSLATIONS mapping in const.py."""

    def test_all_expected_risk_types_present(self):
        """All expected risk types are mapped."""
        expected_types = {
            "legacy_risk",
            "new_mandatory",
            "missing_input",
            "removed_input",
            "selector_mismatch",
            "compatibility_risk",
            "validation_failed_blueprint",
            "system_error",
        }
        assert expected_types == set(RISK_TYPE_TRANSLATIONS.keys())

    def test_translation_key_values_are_strings(self):
        """All translation keys map to non-empty strings."""
        for risk_type, key in RISK_TYPE_TRANSLATIONS.items():
            assert isinstance(key, str), f"Key for {risk_type} is not a string"
            assert len(key) > 0, f"Key for {risk_type} is empty"

    def test_translation_keys_start_with_risk(self):
        """All translation value strings start with 'risk_'."""
        for risk_type, key in RISK_TYPE_TRANSLATIONS.items():
            assert key.startswith("risk_"), f"Translation key '{key}' for '{risk_type}' doesn't start with 'risk_'"

    def test_no_duplicate_translation_values(self):
        """No two risk types map to the same translation key."""
        values = list(RISK_TYPE_TRANSLATIONS.values())
        assert len(values) == len(set(values)), "Duplicate translation keys found"

    def test_specific_mapping_legacy_risk(self):
        """legacy_risk maps to risk_legacy."""
        assert RISK_TYPE_TRANSLATIONS["legacy_risk"] == "risk_legacy"

    def test_specific_mapping_compatibility_risk(self):
        """compatibility_risk maps to risk_compatibility."""
        assert RISK_TYPE_TRANSLATIONS["compatibility_risk"] == "risk_compatibility"

    def test_specific_mapping_system_error(self):
        """system_error maps to risk_system_error."""
        assert RISK_TYPE_TRANSLATIONS["system_error"] == "risk_system_error"

    def test_specific_mapping_validation_failed(self):
        """validation_failed_blueprint maps to risk_validation_failed_blueprint."""
        assert RISK_TYPE_TRANSLATIONS["validation_failed_blueprint"] == "risk_validation_failed_blueprint"


# ---------------------------------------------------------------------------
# _extract_inputs_schema
# ---------------------------------------------------------------------------


SIMPLE_BLUEPRINT_YAML = """
blueprint:
  name: Test
  domain: automation
  input:
    required_input:
      name: Required
      selector:
        entity: {}
    optional_input:
      name: Optional
      default: "some_default"
      selector:
        text: {}
"""

BLUEPRINT_WITH_SECTION_YAML = """
blueprint:
  name: Sectioned
  domain: automation
  input:
    section_one:
      name: Section
      input:
        nested_required:
          name: Nested
          selector:
            entity: {}
        nested_optional:
          name: Nested Optional
          default: "val"
"""

BLUEPRINT_NO_INPUTS_YAML = """
blueprint:
  name: No Inputs
  domain: automation
"""

INVALID_YAML = "not: a: valid: blueprint: {["


class TestExtractInputsSchema:
    """Tests for BlueprintUpdateCoordinator._extract_inputs_schema."""

    def test_extracts_mandatory_input(self):
        """Correctly identifies mandatory inputs (no default)."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(SIMPLE_BLUEPRINT_YAML)
        assert err is None
        assert "required_input" in schema
        assert schema["required_input"]["mandatory"] is True

    def test_extracts_optional_input(self):
        """Correctly identifies optional inputs (has default)."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(SIMPLE_BLUEPRINT_YAML)
        assert err is None
        assert "optional_input" in schema
        assert schema["optional_input"]["mandatory"] is False

    def test_extracts_selector_type(self):
        """Correctly extracts selector type from input definition."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(SIMPLE_BLUEPRINT_YAML)
        assert err is None
        assert schema["required_input"]["selector"] == "entity"
        assert schema["optional_input"]["selector"] == "text"

    def test_no_selector_returns_none(self):
        """Input without selector has selector=None."""
        yaml_content = """
blueprint:
  name: Test
  domain: automation
  input:
    plain_input:
      name: Plain
"""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(yaml_content)
        assert err is None
        assert schema["plain_input"]["selector"] is None

    def test_nested_section_inputs_flattened(self):
        """Inputs nested inside sections are flattened into the schema."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(BLUEPRINT_WITH_SECTION_YAML)
        assert err is None
        assert "nested_required" in schema
        assert "nested_optional" in schema
        assert "section_one" not in schema

    def test_nested_required_correctly_classified(self):
        """Nested required input is classified as mandatory."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(BLUEPRINT_WITH_SECTION_YAML)
        assert err is None
        assert schema["nested_required"]["mandatory"] is True
        assert schema["nested_optional"]["mandatory"] is False

    def test_no_inputs_returns_empty_schema(self):
        """Blueprint with no inputs returns empty schema without error."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(BLUEPRINT_NO_INPUTS_YAML)
        assert err is None
        assert schema == {}

    def test_non_dict_blueprint_returns_empty_schema(self):
        """Non-dict blueprint content returns empty schema."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema("just a string")
        assert schema == {}
        assert err is None

    def test_missing_blueprint_key_returns_empty_schema(self):
        """YAML without 'blueprint' key returns empty schema."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema("key: value")
        assert schema == {}
        assert err is None

    def test_blueprint_key_not_dict_returns_empty_schema(self):
        """'blueprint' key that is not a dict returns empty schema."""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema("blueprint: not_a_dict")
        assert schema == {}
        assert err is None

    def test_inputs_not_dict_returns_empty_schema(self):
        """Blueprint with 'input' that is not a dict returns empty schema."""
        yaml_content = """
blueprint:
  name: Test
  input:
    - not
    - a
    - dict
"""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(yaml_content)
        assert schema == {}
        assert err is None

    def test_non_dict_input_value_treated_as_mandatory(self):
        """Input value that is not a dict (e.g., null) treated as mandatory."""
        yaml_content = """
blueprint:
  name: Test
  input:
    plain_key:
"""
        schema, err = BlueprintUpdateCoordinator._extract_inputs_schema(yaml_content)
        assert err is None
        assert "plain_key" in schema
        assert schema["plain_key"]["mandatory"] is True
        assert schema["plain_key"]["selector"] is None


# ---------------------------------------------------------------------------
# _populate_config_from_entity (static method)
# ---------------------------------------------------------------------------


class TestPopulateConfigFromEntity:
    """Tests for BlueprintUpdateCoordinator._populate_config_from_entity."""

    def test_uses_raw_config_when_available(self):
        """Prefers raw_config over config attribute."""
        entity = MagicMock()
        entity.raw_config = {
            "use_blueprint": {"path": "test.yaml", "input": {"key": "value"}}
        }
        configs: dict[str, Any] = {}
        BlueprintUpdateCoordinator._populate_config_from_entity(entity, "automation.test", configs)
        assert "automation.test" in configs
        assert configs["automation.test"]["use_blueprint"]["input"]["key"] == "value"

    def test_falls_back_to_config_attribute(self):
        """Falls back to 'config' attribute when raw_config is absent."""
        entity = MagicMock()
        entity.raw_config = None
        entity.config = {
            "use_blueprint": {"path": "test.yaml", "input": {}}
        }
        configs: dict[str, Any] = {}
        BlueprintUpdateCoordinator._populate_config_from_entity(entity, "automation.test", configs)
        assert "automation.test" in configs

    def test_skips_entity_without_use_blueprint(self):
        """Entities without 'use_blueprint' in config are skipped."""
        entity = MagicMock()
        entity.raw_config = {"trigger": []}
        configs: dict[str, Any] = {}
        BlueprintUpdateCoordinator._populate_config_from_entity(entity, "automation.test", configs)
        assert "automation.test" not in configs

    def test_skips_entity_with_non_dict_raw_config(self):
        """Non-dict raw_config is ignored, falls back to config."""
        entity = MagicMock()
        entity.raw_config = "not_a_dict"
        entity.config = None
        configs: dict[str, Any] = {}
        BlueprintUpdateCoordinator._populate_config_from_entity(entity, "automation.test", configs)
        assert "automation.test" not in configs

    def test_does_not_override_existing_config(self):
        """If entity_id already in configs, does not overwrite (based on current logic it does)."""
        entity = MagicMock()
        entity.raw_config = {"use_blueprint": {"path": "test.yaml", "input": {"key": "new"}}}
        configs: dict[str, Any] = {"automation.test": {"use_blueprint": {"input": {"key": "old"}}}}
        BlueprintUpdateCoordinator._populate_config_from_entity(entity, "automation.test", configs)
        # It should overwrite since it just assigns
        assert configs["automation.test"]["use_blueprint"]["input"]["key"] == "new"


# ---------------------------------------------------------------------------
# _get_affected_entities (static method)
# ---------------------------------------------------------------------------


class TestGetAffectedEntities:
    """Tests for BlueprintUpdateCoordinator._get_affected_entities."""

    def _make_configs(self) -> dict[str, dict[str, Any]]:
        """Build sample entity configs."""
        return {
            "automation.uses_key": {
                "use_blueprint": {
                    "path": "test.yaml",
                    "input": {"my_key": "value", "other_key": "val2"},
                }
            },
            "automation.no_key": {
                "use_blueprint": {
                    "path": "test.yaml",
                    "input": {"other_key": "val"},
                }
            },
            "automation.no_use_blueprint": {
                "trigger": []
            },
        }

    def test_returns_entities_using_key(self):
        """Returns entity IDs that use the given input key."""
        configs = self._make_configs()
        affected = BlueprintUpdateCoordinator._get_affected_entities(configs, "my_key")
        assert "automation.uses_key" in affected
        assert "automation.no_key" not in affected

    def test_returns_empty_for_missing_key(self):
        """Returns empty list when no entity uses the given key."""
        configs = self._make_configs()
        affected = BlueprintUpdateCoordinator._get_affected_entities(configs, "nonexistent_key")
        assert affected == []

    def test_excludes_entity_without_use_blueprint(self):
        """Entities without 'use_blueprint' are excluded."""
        configs = self._make_configs()
        affected = BlueprintUpdateCoordinator._get_affected_entities(configs, "trigger")
        assert "automation.no_use_blueprint" not in affected

    def test_excludes_entity_with_non_dict_input(self):
        """Entity where use_blueprint.input is not a dict is excluded."""
        configs = {
            "automation.bad": {
                "use_blueprint": {"path": "test.yaml", "input": "not_a_dict"}
            }
        }
        affected = BlueprintUpdateCoordinator._get_affected_entities(configs, "some_key")
        assert affected == []

    def test_returns_all_entities_using_key(self):
        """Returns all entities using the specified key."""
        configs = {
            "automation.first": {"use_blueprint": {"input": {"shared_key": "a"}}},
            "automation.second": {"use_blueprint": {"input": {"shared_key": "b"}}},
        }
        affected = BlueprintUpdateCoordinator._get_affected_entities(configs, "shared_key")
        assert len(affected) == 2


# ---------------------------------------------------------------------------
# _detect_new_mandatory_inputs (static method)
# ---------------------------------------------------------------------------


class TestDetectNewMandatoryInputs:
    """Tests for BlueprintUpdateCoordinator._detect_new_mandatory_inputs."""

    def test_detects_brand_new_mandatory_input(self):
        """Detects an input that is new and mandatory."""
        old_schema: dict[str, Any] = {}
        new_schema: dict[str, Any] = {"new_key": {"mandatory": True, "selector": None}}
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs(old_schema, new_schema)
        assert len(risks) == 1
        assert risks[0]["type"] == "new_mandatory"
        assert risks[0]["args"]["input"] == "new_key"

    def test_no_risk_for_new_optional_input(self):
        """New optional input is not flagged as a risk."""
        old_schema: dict[str, Any] = {}
        new_schema: dict[str, Any] = {"new_key": {"mandatory": False, "selector": None}}
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs(old_schema, new_schema)
        assert risks == []

    def test_detects_optional_becoming_mandatory(self):
        """Input that was optional and becomes mandatory is flagged."""
        old_schema: dict[str, Any] = {"key": {"mandatory": False, "selector": None}}
        new_schema: dict[str, Any] = {"key": {"mandatory": True, "selector": None}}
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs(old_schema, new_schema)
        assert len(risks) == 1
        assert risks[0]["args"]["input"] == "key"

    def test_no_risk_when_mandatory_input_unchanged(self):
        """Existing mandatory input that stays mandatory is not flagged."""
        old_schema: dict[str, Any] = {"key": {"mandatory": True, "selector": None}}
        new_schema: dict[str, Any] = {"key": {"mandatory": True, "selector": None}}
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs(old_schema, new_schema)
        assert risks == []

    def test_no_risk_when_schemas_are_identical(self):
        """No risks when old and new schemas are identical."""
        schema = {"key1": {"mandatory": True}, "key2": {"mandatory": False}}
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs(schema, schema)
        assert risks == []

    def test_returns_empty_for_empty_schemas(self):
        """No risks when both schemas are empty."""
        risks = BlueprintUpdateCoordinator._detect_new_mandatory_inputs({}, {})
        assert risks == []


# ---------------------------------------------------------------------------
# _detect_missing_inputs (static method)
# ---------------------------------------------------------------------------


class TestDetectMissingInputs:
    """Tests for BlueprintUpdateCoordinator._detect_missing_inputs."""

    def _make_entity_config(self, entity_id: str, inputs: dict) -> dict[str, Any]:
        """Create a minimal entity config with use_blueprint."""
        return {
            entity_id: {
                "use_blueprint": {"path": "test.yaml", "input": inputs}
            }
        }

    def test_detects_missing_mandatory_input(self):
        """Flags entity that is missing a mandatory input."""
        new_schema: dict[str, Any] = {
            "required_key": {"mandatory": True, "selector": None},
        }
        configs = self._make_entity_config("automation.test", {})
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert len(risks) == 1
        assert risks[0]["type"] == "missing_input"
        assert risks[0]["args"]["entity"] == "automation.test"
        assert risks[0]["args"]["input"] == "required_key"

    def test_no_risk_when_entity_provides_all_inputs(self):
        """No risk when entity provides all required inputs."""
        new_schema: dict[str, Any] = {
            "required_key": {"mandatory": True, "selector": None},
        }
        configs = self._make_entity_config("automation.test", {"required_key": "value"})
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert risks == []

    def test_no_risk_for_optional_input(self):
        """No risk when entity doesn't provide an optional input."""
        new_schema: dict[str, Any] = {
            "optional_key": {"mandatory": False, "selector": None},
        }
        configs = self._make_entity_config("automation.test", {})
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert risks == []

    def test_skips_entity_without_use_blueprint(self):
        """Skips entities without 'use_blueprint' in config."""
        new_schema: dict[str, Any] = {"required": {"mandatory": True}}
        configs = {"automation.test": {"trigger": []}}
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert risks == []

    def test_skips_entity_with_non_dict_input(self):
        """Skips entities where use_blueprint.input is not a dict."""
        new_schema: dict[str, Any] = {"required": {"mandatory": True}}
        configs = {"automation.test": {"use_blueprint": {"input": "not_a_dict"}}}
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert risks == []

    def test_multiple_missing_inputs_per_entity(self):
        """Detects multiple missing inputs for a single entity."""
        new_schema: dict[str, Any] = {
            "input_a": {"mandatory": True},
            "input_b": {"mandatory": True},
        }
        configs = self._make_entity_config("automation.test", {})
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert len(risks) == 2

    def test_multiple_entities_checked(self):
        """Checks multiple entities independently."""
        new_schema: dict[str, Any] = {"required": {"mandatory": True}}
        configs = {
            "automation.one": {"use_blueprint": {"input": {}}},
            "automation.two": {"use_blueprint": {"input": {}}},
        }
        risks = BlueprintUpdateCoordinator._detect_missing_inputs(new_schema, configs)
        assert len(risks) == 2
        entity_ids = {r["args"]["entity"] for r in risks}
        assert "automation.one" in entity_ids
        assert "automation.two" in entity_ids


# ---------------------------------------------------------------------------
# _detect_removed_inputs
# ---------------------------------------------------------------------------


class TestDetectRemovedInputs:
    """Tests for BlueprintUpdateCoordinator._detect_removed_inputs."""

    def setup_method(self):
        """Set up coordinator instance."""
        entry = MagicMock()
        entry.domain = DOMAIN
        hass = MagicMock()
        hass.data = {}
        with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
            self.coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        self.coordinator.data = {}

    def _make_config_with_key(self, key: str) -> dict[str, Any]:
        """Build entity config using the given input key."""
        return {
            "automation.test": {
                "use_blueprint": {"input": {key: "some_value"}}
            }
        }

    def test_detects_removed_input_in_use(self):
        """Flags removed input that is still used by entities."""
        old_schema = {"removed_key": {"mandatory": True}}
        new_schema: dict[str, Any] = {}
        configs = self._make_config_with_key("removed_key")
        risks = self.coordinator._detect_removed_inputs(old_schema, new_schema, configs)
        assert len(risks) == 1
        assert risks[0]["type"] == "removed_input"
        assert risks[0]["args"]["input"] == "removed_key"
        assert risks[0]["args"]["count"] == 1

    def test_no_risk_for_removed_input_not_in_use(self):
        """No risk when removed input is not used by any entity."""
        old_schema = {"removed_key": {"mandatory": False}}
        new_schema: dict[str, Any] = {}
        configs: dict[str, Any] = {}
        risks = self.coordinator._detect_removed_inputs(old_schema, new_schema, configs)
        assert risks == []

    def test_no_risk_when_input_still_present(self):
        """No risk when input is present in both old and new schema."""
        schema = {"kept_key": {"mandatory": True}}
        configs = self._make_config_with_key("kept_key")
        risks = self.coordinator._detect_removed_inputs(schema, schema, configs)
        assert risks == []

    def test_count_reflects_number_of_affected_entities(self):
        """Risk count correctly reflects how many entities use the removed input."""
        old_schema = {"shared_key": {"mandatory": True}}
        new_schema: dict[str, Any] = {}
        configs = {
            "automation.a": {"use_blueprint": {"input": {"shared_key": "v1"}}},
            "automation.b": {"use_blueprint": {"input": {"shared_key": "v2"}}},
        }
        risks = self.coordinator._detect_removed_inputs(old_schema, new_schema, configs)
        assert risks[0]["args"]["count"] == 2


# ---------------------------------------------------------------------------
# _detect_selector_mismatches
# ---------------------------------------------------------------------------


class TestDetectSelectorMismatches:
    """Tests for BlueprintUpdateCoordinator._detect_selector_mismatches."""

    def setup_method(self):
        """Set up coordinator instance."""
        entry = MagicMock()
        entry.domain = DOMAIN
        hass = MagicMock()
        hass.data = {}
        with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
            self.coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        self.coordinator.data = {}

    def test_detects_selector_type_change(self):
        """Flags input whose selector type changed, when entities are affected."""
        old_schema = {"my_input": {"mandatory": True, "selector": "entity"}}
        new_schema = {"my_input": {"mandatory": True, "selector": "text"}}
        configs = {
            "automation.test": {
                "use_blueprint": {"input": {"my_input": "some_entity"}}
            }
        }
        risks = self.coordinator._detect_selector_mismatches(old_schema, new_schema, configs)
        assert len(risks) == 1
        assert risks[0]["type"] == "selector_mismatch"
        assert risks[0]["args"]["input"] == "my_input"
        assert risks[0]["args"]["old_type"] == "entity"
        assert risks[0]["args"]["new_type"] == "text"

    def test_no_risk_when_selector_unchanged(self):
        """No risk when selector type is the same in both schemas."""
        schema = {"my_input": {"mandatory": True, "selector": "entity"}}
        configs = {"automation.test": {"use_blueprint": {"input": {"my_input": "val"}}}}
        risks = self.coordinator._detect_selector_mismatches(schema, schema, configs)
        assert risks == []

    def test_no_risk_when_no_entities_use_changed_input(self):
        """No risk when selector changed but no entities use that input."""
        old_schema = {"my_input": {"mandatory": True, "selector": "entity"}}
        new_schema = {"my_input": {"mandatory": True, "selector": "text"}}
        risks = self.coordinator._detect_selector_mismatches(old_schema, new_schema, {})
        assert risks == []

    def test_selector_none_to_type_uses_none_string(self):
        """When old selector is None, uses 'none' string in risk args."""
        old_schema = {"my_input": {"mandatory": True, "selector": None}}
        new_schema = {"my_input": {"mandatory": True, "selector": "entity"}}
        configs = {"automation.test": {"use_blueprint": {"input": {"my_input": "val"}}}}
        risks = self.coordinator._detect_selector_mismatches(old_schema, new_schema, configs)
        assert len(risks) == 1
        assert risks[0]["args"]["old_type"] == "none"
        assert risks[0]["args"]["new_type"] == "entity"

    def test_count_reflects_affected_entities(self):
        """Risk args count reflects how many entities are affected."""
        old_schema = {"inp": {"mandatory": True, "selector": "entity"}}
        new_schema = {"inp": {"mandatory": True, "selector": "number"}}
        configs = {
            "automation.a": {"use_blueprint": {"input": {"inp": "v1"}}},
            "automation.b": {"use_blueprint": {"input": {"inp": "v2"}}},
        }
        risks = self.coordinator._detect_selector_mismatches(old_schema, new_schema, configs)
        assert risks[0]["args"]["count"] == 2


# ---------------------------------------------------------------------------
# _dedupe_risks
# ---------------------------------------------------------------------------


class TestDedupeRisks:
    """Tests for BlueprintUpdateCoordinator._dedupe_risks."""

    def setup_method(self):
        """Set up coordinator instance."""
        entry = MagicMock()
        entry.domain = DOMAIN
        hass = MagicMock()
        hass.data = {}
        with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
            self.coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        self.coordinator.data = {}

    def test_deduplicates_identical_structured_risks(self):
        """Removes duplicate structured risks."""
        risk: StructuredRisk = {"type": "new_mandatory", "args": {"input": "key1"}}
        result = self.coordinator._dedupe_risks([risk, risk, risk])
        assert len(result) == 1

    def test_preserves_distinct_structured_risks(self):
        """Keeps distinct risks unchanged."""
        risks: list[StructuredRisk] = [
            {"type": "new_mandatory", "args": {"input": "key1"}},
            {"type": "new_mandatory", "args": {"input": "key2"}},
        ]
        result = self.coordinator._dedupe_risks(risks)
        assert len(result) == 2

    def test_converts_string_risks_to_legacy_format(self):
        """String risks are wrapped in legacy_risk structure."""
        result = self.coordinator._dedupe_risks(["some error string"])
        assert len(result) == 1
        assert result[0]["type"] == "legacy_risk"
        assert result[0]["args"]["message"] == "some error string"

    def test_deduplicates_identical_string_risks(self):
        """Duplicate string risks produce only one legacy_risk entry."""
        result = self.coordinator._dedupe_risks(["error", "error", "error"])
        assert len(result) == 1
        assert result[0]["type"] == "legacy_risk"

    def test_skips_malformed_risks(self):
        """Malformed risks (missing type or args) are silently skipped."""
        malformed = [
            {"type": "valid", "args": {"key": "val"}},
            {"type": "no_args_key"},  # type: ignore[typeddict-item]
            {"args": {"key": "val"}},  # type: ignore[typeddict-item]
            "string risk",
        ]
        result = self.coordinator._dedupe_risks(malformed)
        # Should have: one valid structured + one legacy string
        assert len(result) == 2

    def test_empty_input_returns_empty_list(self):
        """Empty iterable returns empty list."""
        assert self.coordinator._dedupe_risks([]) == []

    def test_order_preserved_for_first_occurrence(self):
        """The first occurrence of each risk is preserved in order."""
        risks: list[StructuredRisk] = [
            {"type": "removed_input", "args": {"input": "a", "count": 1}},
            {"type": "new_mandatory", "args": {"input": "b"}},
            {"type": "removed_input", "args": {"input": "a", "count": 1}},  # duplicate
        ]
        result = self.coordinator._dedupe_risks(risks)
        assert len(result) == 2
        assert result[0]["type"] == "removed_input"
        assert result[1]["type"] == "new_mandatory"

    def test_args_key_order_does_not_affect_deduplication(self):
        """Risks with same content but different arg key order are deduplicated."""
        risk1: StructuredRisk = {"type": "selector_mismatch", "args": {"input": "a", "count": 1, "old_type": "x", "new_type": "y"}}
        risk2: StructuredRisk = {"type": "selector_mismatch", "args": {"count": 1, "new_type": "y", "input": "a", "old_type": "x"}}
        result = self.coordinator._dedupe_risks([risk1, risk2])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _detect_breaking_changes
# ---------------------------------------------------------------------------


class TestDetectBreakingChanges:
    """Tests for BlueprintUpdateCoordinator._detect_breaking_changes."""

    def setup_method(self):
        """Set up coordinator instance."""
        entry = MagicMock()
        entry.domain = DOMAIN
        hass = MagicMock()
        hass.data = {}
        with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
            self.coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        self.coordinator.data = {}

    def test_returns_empty_when_schemas_unchanged(self):
        """Returns no risks when old and new content are identical."""
        with patch.object(self.coordinator, "_get_entities_using_blueprint_list", return_value=[]):
            with patch.object(self.coordinator, "_get_entities_configs", return_value={}):
                risks = self.coordinator._detect_breaking_changes(
                    SIMPLE_BLUEPRINT_YAML, SIMPLE_BLUEPRINT_YAML, "automation/test.yaml"
                )
        assert risks == []

    def test_detects_new_mandatory_input(self):
        """Detects when a new mandatory input is added."""
        old_yaml = SIMPLE_BLUEPRINT_YAML
        new_yaml = SIMPLE_BLUEPRINT_YAML + "\n    new_required:\n      name: New Required\n"
        with patch.object(self.coordinator, "_get_entities_using_blueprint_list", return_value=[]):
            with patch.object(self.coordinator, "_get_entities_configs", return_value={}):
                risks = self.coordinator._detect_breaking_changes(
                    old_yaml, new_yaml, "automation/test.yaml"
                )
        risk_types = [r["type"] for r in risks]
        assert "new_mandatory" in risk_types

    def test_returns_validation_failed_when_old_content_invalid(self):
        """Returns validation_failed_blueprint risk when old content cannot be parsed."""
        with patch.object(
            BlueprintUpdateCoordinator,
            "_extract_inputs_schema",
            side_effect=[({}, "parse error"), ({}, None)],
        ):
            risks = self.coordinator._detect_breaking_changes(
                "bad yaml", SIMPLE_BLUEPRINT_YAML, "automation/test.yaml"
            )
        assert len(risks) == 1
        assert risks[0]["type"] == "validation_failed_blueprint"

    def test_returns_validation_failed_when_new_content_invalid(self):
        """Returns validation_failed_blueprint risk when new content cannot be parsed."""
        with patch.object(
            BlueprintUpdateCoordinator,
            "_extract_inputs_schema",
            side_effect=[({}, None), ({}, "parse error")],
        ):
            risks = self.coordinator._detect_breaking_changes(
                SIMPLE_BLUEPRINT_YAML, "bad yaml", "automation/test.yaml"
            )
        assert len(risks) == 1
        assert risks[0]["type"] == "validation_failed_blueprint"


# ---------------------------------------------------------------------------
# _get_entities_using_blueprint_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entities_using_blueprint_list_automation(coordinator):
    """Returns automation entities for blueprint in automation domain."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.automations_with_blueprint",
            return_value=["automation.test_one", "automation.test_two"],
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.scripts_with_blueprint",
            return_value=[],
        ),
    ):
        result = coordinator._get_entities_using_blueprint_list("automation/test.yaml")

    assert "automation.test_one" in result
    assert "automation.test_two" in result


@pytest.mark.asyncio
async def test_get_entities_using_blueprint_list_script(coordinator):
    """Returns script entities for blueprint in script domain."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.automations_with_blueprint",
            return_value=[],
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.scripts_with_blueprint",
            return_value=["script.test_script"],
        ),
    ):
        result = coordinator._get_entities_using_blueprint_list("script/test.yaml")

    assert "script.test_script" in result


@pytest.mark.asyncio
async def test_get_entities_using_blueprint_list_deduplicates(coordinator):
    """Deduplicates entities that appear in both automation and script results."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.automations_with_blueprint",
            return_value=["automation.shared"],
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.scripts_with_blueprint",
            return_value=["automation.shared"],
        ),
    ):
        result = coordinator._get_entities_using_blueprint_list("unknown/test.yaml")

    assert result.count("automation.shared") == 1


# ---------------------------------------------------------------------------
# _update_coordinator_status_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_coordinator_status_data_with_error(coordinator):
    """Sets error state fields correctly when last_error is set."""
    coordinator.data["test/path.yaml"] = {"updatable": True}
    coordinator._update_coordinator_status_data(
        "test/path.yaml",
        updatable=False,
        last_error="yaml_syntax_error|some error",
        remote_hash="abc123",
        remote_content="content",
        new_etag="etag1",
    )
    entry = coordinator.data["test/path.yaml"]
    assert entry["last_error"] == "yaml_syntax_error|some error"
    assert entry["invalid_remote_hash"] == "abc123"
    assert entry["remote_hash"] is None
    assert entry["remote_content"] is None
    assert entry["updatable"] is False
    assert entry["update_blocking_reason"] is None
    assert entry["breaking_risks"] == []


@pytest.mark.asyncio
async def test_update_coordinator_status_data_without_error_updatable(coordinator):
    """Sets updatable state correctly when no error and updatable=True."""
    coordinator.data["test/path.yaml"] = {}
    coordinator._update_coordinator_status_data(
        "test/path.yaml",
        updatable=True,
        last_error=None,
        remote_hash="abc123",
        remote_content="new content",
        new_etag="etag1",
    )
    entry = coordinator.data["test/path.yaml"]
    assert entry["updatable"] is True
    assert entry["remote_hash"] == "abc123"
    assert entry["remote_content"] == "new content"
    assert entry["last_error"] is None
    assert entry["invalid_remote_hash"] is None
    assert entry["update_blocking_reason"] is None
    assert entry["breaking_risks"] == []


@pytest.mark.asyncio
async def test_update_coordinator_status_data_without_error_not_updatable(coordinator):
    """remote_content is None when updatable=False and no error."""
    coordinator.data["test/path.yaml"] = {}
    coordinator._update_coordinator_status_data(
        "test/path.yaml",
        updatable=False,
        last_error=None,
        remote_hash="abc123",
        remote_content="content",
        new_etag=None,
    )
    entry = coordinator.data["test/path.yaml"]
    assert entry["updatable"] is False
    assert entry["remote_content"] is None


@pytest.mark.asyncio
async def test_update_coordinator_status_data_does_nothing_if_path_missing(coordinator):
    """Does nothing when path is not in coordinator data."""
    coordinator.data = {}  # empty data
    # Should not raise
    coordinator._update_coordinator_status_data(
        "nonexistent/path.yaml",
        updatable=True,
        last_error=None,
        remote_hash="hash",
        remote_content="content",
        new_etag=None,
    )
    assert "nonexistent/path.yaml" not in coordinator.data


# ---------------------------------------------------------------------------
# _async_send_auto_update_notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_send_auto_update_notification_calls_service(coordinator):
    """Calls persistent_notification.create with correct arguments."""
    await coordinator._async_send_auto_update_notification(
        title="Test Title",
        message="Test Message",
        blueprint_id="my_bp",
        source_unique_id="source_id",
    )
    coordinator.hass.services.async_call.assert_called_once()
    call_args = coordinator.hass.services.async_call.call_args
    assert call_args[0][0] == "persistent_notification"
    assert call_args[0][1] == "create"
    payload = call_args[0][2]
    assert payload["title"] == "Test Title"
    assert payload["message"] == "Test Message"
    assert "notification_id" in payload


@pytest.mark.asyncio
async def test_async_send_auto_update_notification_id_with_source_and_blueprint(coordinator):
    """Notification ID is built from source_unique_id and blueprint_id."""
    await coordinator._async_send_auto_update_notification(
        title="Title",
        message="Msg",
        blueprint_id="bp_id",
        source_unique_id="source_123",
    )
    payload = coordinator.hass.services.async_call.call_args[0][2]
    assert "source_123" in payload["notification_id"]
    assert "bp_id" in payload["notification_id"]


@pytest.mark.asyncio
async def test_async_send_auto_update_notification_id_without_source(coordinator):
    """Notification ID uses blueprint_id when source_unique_id is None."""
    await coordinator._async_send_auto_update_notification(
        title="Title",
        message="Msg",
        blueprint_id="my_bp",
        source_unique_id=None,
    )
    payload = coordinator.hass.services.async_call.call_args[0][2]
    assert "my_bp" in payload["notification_id"]


@pytest.mark.asyncio
async def test_async_send_auto_update_notification_handles_service_error(coordinator):
    """Does not raise when service call fails."""
    coordinator.hass.services.async_call = AsyncMock(side_effect=Exception("Service down"))
    # Should not raise
    await coordinator._async_send_auto_update_notification("Title", "Msg")


# ---------------------------------------------------------------------------
# _handle_auto_update_step: system_error risk type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_update_guard_blocks_on_system_error_risk(coordinator):
    """Auto-update is blocked when a system_error risk is present, even with no consumers."""
    blueprint_path = "automation/test_system_error.yaml"
    await _prepare_blueprint_entry(coordinator, blueprint_path)

    with (
        patch.object(coordinator, "_get_entities_using_blueprint", return_value=[]),
        patch.object(coordinator, "async_translate", AsyncMock(return_value="translated")),
        patch.object(coordinator, "_get_risk_summary", AsyncMock(return_value="summary")),
        patch.object(coordinator, "_async_send_auto_update_notification", AsyncMock()),
    ):
        risks: list[StructuredRisk] = [
            {"type": "system_error", "args": {"error": "missing_path", "path": blueprint_path}}
        ]
        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            "new_content",
            "new_hash",
            "new_etag",
            risks,
            [],
            set(),
        )

    assert result is True
    assert coordinator.data[blueprint_path]["update_blocking_reason"] == "auto_update_blocked_by_breaking_change"


@pytest.mark.asyncio
async def test_auto_update_proceeds_when_no_risks(coordinator):
    """Auto-update proceeds when risk list is empty."""
    blueprint_path = "automation/no_risks.yaml"
    await _prepare_blueprint_entry(coordinator, blueprint_path)

    results_to_notify: list[str] = []
    updated_domains: set[str] = set()

    with patch.object(coordinator, "async_install_blueprint", AsyncMock(return_value=None)):
        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            "new_content",
            "new_hash",
            "new_etag",
            [],
            results_to_notify,
            updated_domains,
        )

    assert result is True
    assert coordinator.data[blueprint_path]["updatable"] is False
    assert coordinator.data[blueprint_path]["local_hash"] == "new_hash"
    assert "Test Blueprint" in results_to_notify
    assert "automation" in updated_domains


@pytest.mark.asyncio
async def test_auto_update_returns_false_on_install_failure(coordinator):
    """Returns False when blueprint installation raises an exception."""
    blueprint_path = "automation/fail_install.yaml"
    await _prepare_blueprint_entry(coordinator, blueprint_path)

    with patch.object(
        coordinator, "async_install_blueprint", AsyncMock(side_effect=Exception("disk full"))
    ):
        result = await coordinator._handle_auto_update_step(
            blueprint_path,
            coordinator.data[blueprint_path],
            "content",
            "hash",
            "etag",
            [],
            [],
            set(),
        )

    assert result is False


# ---------------------------------------------------------------------------
# async_summarize_risks: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_summarize_risks_empty_list(coordinator, monkeypatch):
    """Returns empty string for empty risks list."""
    async def fake_translate(key, **kwargs):
        return "translated"
    monkeypatch.setattr(coordinator, "async_translate", fake_translate)

    result = await coordinator.async_summarize_risks([])
    assert result == ""


@pytest.mark.asyncio
async def test_async_summarize_risks_includes_error_for_unknown_type(coordinator, monkeypatch):
    """Injects 'error' into args for unknown risk types."""
    captured_kwargs: list[dict] = []

    async def fake_translate(key, **kwargs):
        captured_kwargs.append(kwargs)
        return "msg"

    monkeypatch.setattr(coordinator, "async_translate", fake_translate)

    risks: list[StructuredRisk] = [
        {"type": "totally_unknown_type", "args": {"some": "data"}}
    ]
    await coordinator.async_summarize_risks(risks)

    assert any("error" in kw for kw in captured_kwargs)


@pytest.mark.asyncio
async def test_get_risk_summary_delegates_to_async_summarize_risks(coordinator, monkeypatch):
    """_get_risk_summary calls async_summarize_risks (legacy shim)."""
    async def fake_summarize(risks):
        return "summary_result"

    monkeypatch.setattr(coordinator, "async_summarize_risks", fake_summarize)
    result = await coordinator._get_risk_summary([])
    assert result == "summary_result"