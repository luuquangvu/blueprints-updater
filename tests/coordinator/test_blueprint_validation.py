"""Unit tests for blueprint_validation module."""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, cast
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.blueprint.const import CONF_INPUT
from homeassistant.const import CONF_DEFAULT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector as ha_selector
from homeassistant.helpers.template import HassLoader, TemplateEnvironment
from homeassistant.util.yaml.objects import Input

from custom_components.blueprints_updater.blueprint_validation import (
    DEFAULT_SELECTOR_FILTER_PATHS,
    MAX_CUSTOM_TEMPLATE_SIZE,
    SelectorRegistryQuickSig,
    SelectorType,
    StructuredRisk,
    _extract_custom_template_paths,
    _inspect_scoped_math,
    _is_dummy_value_valid_for_selector,
    canonicalize_source_url,
    check_ha_template_ast_compatibility,
    coerce_empty_selectors,
    compute_selector_registry_fingerprint,
    compute_selector_schema_fingerprint,
    dedupe_risks,
    derive_dummy_input_value,
    derive_selector_filter_paths,
    detect_missing_inputs,
    detect_new_mandatory_inputs,
    ensure_source_url,
    ensure_source_url_cached,
    extract_blueprint_text,
    extract_defined_inputs,
    extract_input_configs,
    extract_inputs_with_default,
    extract_mandatory_inputs,
    extract_used_inputs,
    generate_dummy_input_value,
    get_affected_entities,
    get_blueprint_block,
    get_blueprint_schema,
    get_cached_selector_registry_quick_sig,
    get_ha_live_selectors,
    get_selector_filter_paths,
    hash_content,
    invalidate_selector_filter_paths_cache,
    is_input_mandatory,
    is_invalid_for_input_default,
    normalize_content,
    read_and_diff,
    stabilize_yaml_structure,
    validate_input_references,
    validate_safe_input_usages,
)
from custom_components.blueprints_updater.const import BlueprintRiskType


def test_compute_selector_schema_fingerprint_basic():
    """Test schema fingerprint computation on various schema structures."""
    schema = vol.Schema({"key": str, vol.Optional("opt", default="default_val"): int})
    fp = compute_selector_schema_fingerprint(schema)
    assert isinstance(fp, tuple)
    assert fp[0] == "vol.Schema"

    fp_dict = compute_selector_schema_fingerprint({"a": 1, "b": "str"})
    assert fp_dict[0] == "dict"

    fp_all = compute_selector_schema_fingerprint(vol.All(str, vol.Length(min=1)))
    assert fp_all[0] == "vol.All"

    fp_any = compute_selector_schema_fingerprint(vol.Any(int, float))
    assert fp_any[0] == "vol.Any"

    fp_set = compute_selector_schema_fingerprint({"val1", "val2"})
    assert fp_set[0] in ("set", "frozenset")

    fp_list = compute_selector_schema_fingerprint([1, 2, 3])
    assert fp_list[0] == "list"

    def dummy_func(*args: object, **kwargs: object) -> object:
        """Dummy func for testing."""
        return args

    p_func = functools.partial(dummy_func, 1)
    fp_partial = compute_selector_schema_fingerprint(p_func)
    assert fp_partial[0] == "partial"

    # Test closure state fingerprinting
    def outer():
        secret = 42

        def inner():
            """Inner func."""
            return secret

        return inner

    closure_func = outer()
    fp_closure = compute_selector_schema_fingerprint(closure_func)
    assert fp_closure[0] == "callable"

    # Test function with attributes in __dict__
    def attr_func():
        """Attr func."""

    attr_func.__dict__["custom_attr"] = "hello"
    fp_attr = compute_selector_schema_fingerprint(attr_func)
    assert fp_attr[0] == "callable"

    # Test class type
    class CustomClass:
        """Custom test class."""

    fp_class = compute_selector_schema_fingerprint(CustomClass)
    assert fp_class[0] == "type"

    # Test arbitrary object
    obj = object()
    fp_obj = compute_selector_schema_fingerprint(obj)
    assert fp_obj[0] == "opaque_obj"

    # Reordered mappings compare equal
    assert compute_selector_schema_fingerprint(
        {"a": 1, "b": "str"}
    ) == compute_selector_schema_fingerprint({"b": "str", "a": 1})
    assert compute_selector_schema_fingerprint(
        vol.Schema({"x": int, "y": str})
    ) == compute_selector_schema_fingerprint(vol.Schema({"y": str, "x": int}))

    # Changes to defaults produce different fingerprints
    fp_def1 = compute_selector_schema_fingerprint(vol.Schema({vol.Optional("k", default=1): int}))
    fp_def2 = compute_selector_schema_fingerprint(vol.Schema({vol.Optional("k", default=2): int}))
    assert fp_def1 != fp_def2

    # Changes to validator bounds produce different fingerprints
    fp_len1 = compute_selector_schema_fingerprint(vol.Length(min=1, max=5))
    fp_len2 = compute_selector_schema_fingerprint(vol.Length(min=1, max=10))
    assert fp_len1 != fp_len2

    fp_range1 = compute_selector_schema_fingerprint(vol.Range(min=0, max=100))
    fp_range2 = compute_selector_schema_fingerprint(vol.Range(min=10, max=100))
    assert fp_range1 != fp_range2

    # Changes to closure state produce different fingerprints
    def make_closure(secret: int):
        def inner():
            """Inner func."""
            return secret

        return inner

    assert compute_selector_schema_fingerprint(
        make_closure(42)
    ) != compute_selector_schema_fingerprint(make_closure(99))

    # Changes to partial arguments produce different fingerprints
    assert compute_selector_schema_fingerprint(
        functools.partial(dummy_func, 1)
    ) != compute_selector_schema_fingerprint(functools.partial(dummy_func, 2))
    assert compute_selector_schema_fingerprint(
        functools.partial(dummy_func, opt=1)
    ) != compute_selector_schema_fingerprint(functools.partial(dummy_func, opt=2))

    # Changes to opaque object attributes produce different fingerprints
    class OpaqueItem:
        """Test opaque object."""

        def __init__(self, val: int) -> None:
            """Initialize test opaque object."""
            self.val = val

    assert compute_selector_schema_fingerprint(
        OpaqueItem(1)
    ) != compute_selector_schema_fingerprint(OpaqueItem(2))


def test_compute_selector_schema_fingerprint_closure_cell_exception():
    """Test fingerprinting handles cell unrepr exception gracefully."""
    mock_cell = MagicMock()

    def _raising_getter(s: object) -> None:
        raise ValueError("boom")

    type(mock_cell).cell_contents = property(_raising_getter)

    class MockCallable:
        """Mock callable object with closure."""

        __closure__: tuple[Any, ...] | None = None

        def __call__(self):
            """Callable body."""

    mock_obj = MockCallable()
    mock_obj.__closure__ = (mock_cell,)
    fp = compute_selector_schema_fingerprint(mock_obj)
    assert fp[0] == "callable"
    extra_items = fp[2]
    assert isinstance(extra_items, tuple)
    closure_items = [
        item for item in extra_items if isinstance(item, tuple) and item[0] == "closure"
    ]
    assert len(closure_items) == 1
    assert ("unrepr_cell",) in closure_items[0][1]


def test_derive_selector_filter_paths_and_registry_fingerprint():
    """Test deriving filter paths from Home Assistant selector registry."""
    fallback = frozenset(
        {(SelectorType.ENTITY, SelectorType.FILTER), (SelectorType.DEVICE, SelectorType.FILTER)}
    )
    assert derive_selector_filter_paths(None, fallback) == fallback
    assert compute_selector_registry_fingerprint(None) is None

    class MockSelector:
        """Mock selector class with CONFIG_SCHEMA."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional(SelectorType.FILTER.value): vol.All(
                    lambda x: x,
                    [vol.Schema({"domain": str})],
                )
            }
        )

    registry = {"test_sel": MockSelector}
    paths = derive_selector_filter_paths(registry, fallback)
    assert isinstance(paths, frozenset)

    fp = compute_selector_registry_fingerprint(registry)
    assert isinstance(fp, tuple)
    assert len(fp) == 1

    # Test controlled registry covering nested, vol.All, and vol.Any schemas
    class MockComplexSelector:
        """Mock selector with nested, vol.All, and vol.Any schemas."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional("top_filter"): vol.All(
                    cv.ensure_list,
                    [vol.Schema({"domain": str})],
                ),
                vol.Optional("nested"): vol.Schema(
                    {
                        vol.Optional("any_filter"): vol.Any(
                            str,
                            vol.All(
                                cv.ensure_list,
                                [vol.Schema({"device": str})],
                            ),
                        )
                    }
                ),
            }
        )

    complex_registry = {"complex_sel": MockComplexSelector}
    complex_paths = derive_selector_filter_paths(complex_registry, default_paths=frozenset())
    assert ("complex_sel", "top_filter") in complex_paths
    assert ("complex_sel", "nested", "any_filter") in complex_paths


def test_derive_selector_filter_paths_live_ha_selectors():
    """Test dynamic discovery on live Home Assistant Core selectors.

    Asserts invariant structural properties rather than static lists.
    """
    discovered = derive_selector_filter_paths(ha_selector.SELECTORS, default_paths=frozenset())
    # Invariant: discovery discovers a frozenset of string-tuple paths from live registry
    assert isinstance(discovered, frozenset)
    assert len(discovered) > 0
    assert all(isinstance(p, tuple) and all(isinstance(seg, str) for seg in p) for p in discovered)

    # Invariant: with default paths, baseline paths are guaranteed to be present
    with_defaults = derive_selector_filter_paths(ha_selector.SELECTORS)
    assert DEFAULT_SELECTOR_FILTER_PATHS.issubset(with_defaults)


def test_selector_filter_paths_caching_and_force_refresh():
    """Test cache fingerprinting, force_refresh, and invalidation with mock registries."""

    class MockSelA:
        """Mock selector A."""

        CONFIG_SCHEMA = vol.Schema(
            {
                vol.Optional(SelectorType.FILTER.value): vol.All(
                    cv.ensure_list, [vol.Schema({"domain": str})]
                )
            }
        )

    class MockSelB:
        """Mock selector B with different filter schema."""

        CONFIG_SCHEMA = vol.Schema(
            {vol.Optional("custom_filter"): vol.All(cv.ensure_list, [vol.Schema({"domain": str})])}
        )

    reg_a = {"sel_a": MockSelA}
    reg_b = {"sel_b": MockSelB}

    def _assert_selector_paths(
        expected_in: tuple[str, str], expected_not_in: tuple[str, str]
    ) -> frozenset[tuple[str, ...]]:
        """Assert selector filter paths contain expected path and omit unexpected path."""
        paths = get_selector_filter_paths()
        assert expected_in in paths
        assert expected_not_in not in paths
        return paths

    with patch.object(ha_selector, "SELECTORS", reg_a):
        invalidate_selector_filter_paths_cache()
        paths_a = _assert_selector_paths(("sel_a", SelectorType.FILTER), ("sel_b", "custom_filter"))

        # Subsequent call hits cache
        assert get_selector_filter_paths() is paths_a

    # With registry B, fingerprint change triggers cache refresh even without force_refresh
    with patch.object(ha_selector, "SELECTORS", reg_b):
        _assert_selector_paths(("sel_b", "custom_filter"), ("sel_a", SelectorType.FILTER))

        # Test force_refresh=True explicitly re-derives
        paths_b_refresh = get_selector_filter_paths(force_refresh=True)
        assert ("sel_b", "custom_filter") in paths_b_refresh

    invalidate_selector_filter_paths_cache()


def test_derive_selector_filter_paths_empty_or_no_paths():
    """Test fallback and warnings when registry is empty or yields no paths."""
    fallback = frozenset({("test", "path")})
    # Empty registry
    assert derive_selector_filter_paths({}, default_paths=fallback) == fallback

    # Registry with selectors that yield no list paths
    class NoListSelector:
        """Selector without list schemas."""

        CONFIG_SCHEMA = vol.Schema({"name": str})

    assert (
        derive_selector_filter_paths({"no_list": NoListSelector}, default_paths=fallback)
        == fallback
    )


def test_generate_dummy_input_value_all_types():
    """Test generating dummy input values for all supported selector types."""
    assert generate_dummy_input_value(None) == "test.dummy"
    assert generate_dummy_input_value("not-a-dict") == "test.dummy"
    assert generate_dummy_input_value({}) == "test.dummy"

    # Entity
    assert generate_dummy_input_value({"entity": None}) == "test.dummy"
    assert generate_dummy_input_value({"entity": {"multiple": True}}) == ["test.dummy"]

    # Target
    assert generate_dummy_input_value({"target": {}}) == {"entity_id": "test.dummy"}

    # Device
    assert generate_dummy_input_value({"device": None}) == "dummy_device_id"
    assert generate_dummy_input_value({"device": {"multiple": True}}) == ["dummy_device_id"]

    # Area
    assert generate_dummy_input_value({"area": None}) == "dummy_area_id"
    assert generate_dummy_input_value({"area": {"multiple": True}}) == ["dummy_area_id"]

    # Floor
    assert generate_dummy_input_value({"floor": None}) == "dummy_floor_id"
    assert generate_dummy_input_value({"floor": {"multiple": True}}) == ["dummy_floor_id"]

    # Label
    assert generate_dummy_input_value({"label": None}) == "dummy_label_id"
    assert generate_dummy_input_value({"label": {"multiple": True}}) == ["dummy_label_id"]

    # Boolean
    assert generate_dummy_input_value({"boolean": None}) is False

    # Number
    assert generate_dummy_input_value({"number": None}) == 0
    assert generate_dummy_input_value({"number": {"min": 5.5}}) == 5.5
    assert generate_dummy_input_value({"number": {"min": "invalid"}}) == 0

    # Text, time, date, datetime
    assert generate_dummy_input_value({"text": None}) == ""
    assert generate_dummy_input_value({"time": None}) == "00:00:00"
    assert generate_dummy_input_value({"date": None}) == "2026-01-01"
    assert generate_dummy_input_value({"datetime": None}) == "2026-01-01 00:00:00"

    # Select
    assert generate_dummy_input_value({"select": {"options": ["opt_a", "opt_b"]}}) == "opt_a"
    assert (
        generate_dummy_input_value(
            {"select": {"options": [{"value": "val_1"}, {"value": "val_2"}]}}
        )
        == "val_1"
    )
    assert generate_dummy_input_value({"select": {"options": ["opt_a"], "multiple": True}}) == [
        "opt_a"
    ]
    assert generate_dummy_input_value({"select": {}}) == "option1"
    assert generate_dummy_input_value({"select": None}) == "option1"

    # Action, color_temp, color_rgb, object, addon, template, duration, trigger, condition, constant
    assert generate_dummy_input_value({"action": None}) == []
    assert generate_dummy_input_value({"trigger": None}) == [
        {"trigger": "state", "entity_id": "test.dummy"}
    ]
    assert generate_dummy_input_value({"condition": None}) == [
        {"condition": "state", "entity_id": "test.dummy", "state": "on"}
    ]
    assert generate_dummy_input_value({"constant": {"value": 42}}) == 42
    assert generate_dummy_input_value({"color_temp": None}) == 300
    assert generate_dummy_input_value({"color_rgb": None}) == [255, 255, 255]
    assert generate_dummy_input_value({"object": None}) == {}
    assert generate_dummy_input_value({"addon": None}) == "core_ssh"
    assert generate_dummy_input_value({"template": None}) == ""
    assert generate_dummy_input_value({"duration": None}) == {
        "hours": 0,
        "minutes": 0,
        "seconds": 0,
    }
    assert generate_dummy_input_value({"location": None}) == {"latitude": 0.0, "longitude": 0.0}
    assert generate_dummy_input_value({"media": None}) == {
        "entity_id": "media_player.dummy",
        "media_content_id": "dummy",
        "media_content_type": "dummy",
    }
    assert generate_dummy_input_value({"media": {"accept": ["audio/*"]}}) == {
        "media_content_id": "dummy",
        "media_content_type": "dummy",
    }
    assert generate_dummy_input_value({"file": None}) == "00000000-0000-0000-0000-000000000000"
    assert generate_dummy_input_value({"backup_location": None}) == "/backup"
    assert generate_dummy_input_value({"icon": None}) == "mdi:home"
    assert generate_dummy_input_value({"theme": None}) == "default"
    assert generate_dummy_input_value({"country": None}) == "US"
    assert generate_dummy_input_value({"country": {"countries": ["CA", "US"]}}) == "CA"
    assert generate_dummy_input_value({"country": {"countries": ["CA"], "multiple": True}}) == [
        "CA"
    ]
    assert generate_dummy_input_value({"language": None}) == "en"
    assert generate_dummy_input_value({"language": {"languages": ["fr", "en"]}}) == "fr"
    assert generate_dummy_input_value({"language": {"languages": ["fr"], "multiple": True}}) == [
        "fr"
    ]
    assert (
        generate_dummy_input_value({"color_temp": {"unit": "kelvin", "min": 2000, "max": 6500}})
        == 3000
    )
    assert (
        generate_dummy_input_value({"color_temp": {"unit": "kelvin", "min": 4000, "max": 6500}})
        == 4000
    )
    assert generate_dummy_input_value({"color_temp": {"min": 400, "max": 500}}) == 400


@pytest.mark.parametrize(
    ("sel_cfg", "expected"),
    [
        # Negative ranges and max-only number selectors
        ({"number": {"max": -5}}, -5.0),
        ({"number": {"min": -10, "max": -2}}, -10.0),
        ({"number": {"min": -5}}, -5.0),
        ({"number": {"max": -1}}, -1.0),
        ({"number": {"min": 0, "max": 10}}, 0.0),
        # Select with options as tuples and non-string scalars
        ({"select": {"options": ("opt1", "opt2")}}, "opt1"),
        ({"select": {"options": ("opt1", "opt2"), "multiple": True}}, ["opt1"]),
        ({"select": {"options": [10, 20]}}, 10),
        ({"select": {"options": (42, 84), "multiple": True}}, [42]),
        ({"select": {"options": [{"value": 100}]}}, 100),
        ({"select": {"options": "not-a-sequence"}}, "option1"),
        ({"select": {"options": ()}}, "option1"),
        # Unknown selectors and empty dictionaries
        ({}, "test.dummy"),
        ({"unknown_type": {}}, "test.dummy"),
        ({"unknown_type": None}, "test.dummy"),
        # Multiple keys in selector dict
        ({"extra_key": True, "number": {"max": -5}}, -5.0),
    ],
)
def test_generate_dummy_input_value_boundaries(sel_cfg: object, expected: object):
    """Test boundary cases for dummy input value generation."""
    assert generate_dummy_input_value(sel_cfg) == expected


def test_derive_dummy_input_value():
    """Test deriving dummy input values from selector and observed blueprint usage."""
    bp_dict: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "trigger": [{"platform": "state", "entity_id": Input("entity_inp")}],
        "condition": [Input("cond_inp")],
        "action": [
            {"action": "light.turn_on", "target": Input("target_inp")},
            Input("action_inp"),
        ],
        "variables": {
            "my_var": Input("var_child"),
        },
    }

    # Input with explicit selector
    cfg_with_selector = {"selector": {"boolean": {}}}
    assert derive_dummy_input_value("bool_inp", cfg_with_selector, bp_dict) is False

    # Location, media, file selectors
    assert derive_dummy_input_value("loc_inp", {"selector": {"location": {}}}, bp_dict) == {
        "latitude": 0.0,
        "longitude": 0.0,
    }
    assert derive_dummy_input_value("media_inp", {"selector": {"media": {}}}, bp_dict) == {
        "entity_id": "media_player.dummy",
        "media_content_id": "dummy",
        "media_content_type": "dummy",
    }
    assert (
        derive_dummy_input_value("file_inp", {"selector": {"file": {"accept": ".txt"}}}, bp_dict)
        == "00000000-0000-0000-0000-000000000000"
    )

    # Unknown selector rejected by schema validation returns None without crashing
    assert (
        derive_dummy_input_value("unknown_inp", {"selector": {"unknown_unsupported": {}}}, bp_dict)
        is None
    )

    # Selectorless target input
    assert derive_dummy_input_value("target_inp", {}, bp_dict) == {"entity_id": "test.dummy"}

    # Selectorless action input (item in action sequence)
    assert derive_dummy_input_value("action_inp", {}, bp_dict) == {
        "action": "homeassistant.update_entity",
        "target": {"entity_id": "test.dummy"},
    }

    # Selectorless entire action block
    bp_action_block: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "action": Input("all_actions"),
    }
    assert derive_dummy_input_value("all_actions", {}, bp_action_block) == []

    # Selectorless condition item inside list
    assert derive_dummy_input_value("cond_inp", {}, bp_dict) == {
        "condition": "state",
        "entity_id": "test.dummy",
        "state": "on",
    }

    # Selectorless entire condition block
    bp_cond_block: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "condition": Input("all_conditions"),
    }
    assert derive_dummy_input_value("all_conditions", {}, bp_cond_block) == [
        {"condition": "state", "entity_id": "test.dummy", "state": "on"}
    ]

    # Selectorless trigger block
    bp_trigger_block: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "trigger": Input("all_triggers"),
    }
    assert derive_dummy_input_value("all_triggers", {}, bp_trigger_block) == [
        {"trigger": "state", "entity_id": "test.dummy"}
    ]

    # Selectorless entity input
    assert derive_dummy_input_value("entity_inp", {}, bp_dict) == "test.dummy"

    # Device ID, Area ID, Floor ID, Label ID inputs
    bp_ids: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "action": [
            {
                "target": {
                    "device_id": Input("dev_inp"),
                    "area_id": Input("area_inp"),
                    "floor_id": Input("floor_inp"),
                    "label_id": Input("label_inp"),
                }
            }
        ],
    }
    assert derive_dummy_input_value("dev_inp", {}, bp_ids) == "dummy_device_id"
    assert derive_dummy_input_value("area_inp", {}, bp_ids) == "dummy_area_id"
    assert derive_dummy_input_value("floor_inp", {}, bp_ids) == "dummy_floor_id"
    assert derive_dummy_input_value("label_inp", {}, bp_ids) == "dummy_label_id"

    # Variables root block and variable child
    bp_vars_block: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "variables": Input("all_vars"),
    }
    assert derive_dummy_input_value("all_vars", {}, bp_vars_block) == {}
    assert derive_dummy_input_value("var_child", {}, bp_dict) == "test.dummy"

    # Unused input
    assert derive_dummy_input_value("unused_inp", {}, bp_dict) == "test.dummy"

    # Underivable usage
    bp_underivable: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "action": [{"delay": Input("delay_inp")}],
        "data": {"weird_unknown": Input("weird_inp")},
    }
    assert derive_dummy_input_value("delay_inp", {}, bp_underivable) is None
    assert derive_dummy_input_value("weird_inp", {}, bp_underivable) is None

    # Conflicting multiple usages
    bp_conflict: dict[str, object] = {
        "blueprint": {"name": "Test", "domain": "automation"},
        "action": [
            Input("conflicting_inp"),
            {"target": Input("conflicting_inp")},
        ],
    }
    assert derive_dummy_input_value("conflicting_inp", {}, bp_conflict) is None


def test_is_invalid_for_input_default():
    """Test detection of default values that fail Home Assistant input schema validation."""
    assert is_invalid_for_input_default(None, {}) is True
    assert is_invalid_for_input_default("", {}) is True
    assert is_invalid_for_input_default([], {}) is True
    assert is_invalid_for_input_default({}, {}) is True

    # Target selector rejects empty values and mappings without target keys
    assert is_invalid_for_input_default({}, {"selector": {"target": {}}}) is True
    assert is_invalid_for_input_default("", {"selector": {"target": {}}}) is True
    assert is_invalid_for_input_default([], {"selector": {"target": {}}}) is True
    target_sel = {"selector": {"target": {}}}
    assert is_invalid_for_input_default({"entity_id": "test"}, target_sel) is False
    assert is_invalid_for_input_default({"entity_id": ""}, target_sel) is True

    # Selectors requiring non-empty values reject empty defaults
    assert is_invalid_for_input_default("", {"selector": {"entity": {}}}) is True
    assert is_invalid_for_input_default([], {"selector": {"entity": {}}}) is True
    assert is_invalid_for_input_default({}, {"selector": {"entity": {}}}) is True
    assert is_invalid_for_input_default("", {"selector": {"device": {}}}) is True
    assert is_invalid_for_input_default("", {"selector": {"area": {}}}) is True
    assert is_invalid_for_input_default("", {"selector": {"floor": {}}}) is True
    assert is_invalid_for_input_default("", {"selector": {"label": {}}}) is True

    # Action selector: empty list is a valid empty action sequence; empty string/dict is not
    assert is_invalid_for_input_default([], {"selector": {"action": {}}}) is False
    assert is_invalid_for_input_default("", {"selector": {"action": {}}}) is True
    assert is_invalid_for_input_default({}, {"selector": {"action": {}}}) is True

    # Legitimate optional selectors allow empty defaults
    assert is_invalid_for_input_default("", {"selector": {"text": {}}}) is False
    assert is_invalid_for_input_default({}, {"selector": {"object": {}}}) is False
    assert is_invalid_for_input_default([], {"selector": {"select": {"multiple": True}}}) is False
    assert is_invalid_for_input_default("", {"selector": {"select": {}}}) is False

    # Valid values
    assert is_invalid_for_input_default("valid_val", {}) is False
    assert is_invalid_for_input_default(0, {}) is False
    assert is_invalid_for_input_default(False, {}) is False
    assert is_invalid_for_input_default([1, 2], {}) is False


def test_validate_safe_input_usages():
    """Test validation of safe input usages in actions, targets, and conditions."""
    # Top-level non-mapping
    assert validate_safe_input_usages("string") == []

    # Blueprint with invalid defaults and empty default inputs
    blueprint_obj = {
        "blueprint": {
            CONF_INPUT: {
                "bad_input": {
                    CONF_DEFAULT: "",
                    "selector": {"entity": {}},
                }
            }
        },
        "action": [
            {
                "target": {"entity_id": Input("bad_input")},
                "service": Input("bad_input"),
            }
        ],
    }
    errors = validate_safe_input_usages(blueprint_obj)
    assert any("bad_input" in err for err in errors)

    # Legitimate optional text/action/object inputs with empty defaults are allowed
    safe_blueprint = {
        "blueprint": {
            CONF_INPUT: {
                "opt_text": {
                    CONF_DEFAULT: "",
                    "selector": {"text": {}},
                },
                "opt_actions": {
                    CONF_DEFAULT: [],
                    "selector": {"action": {}},
                },
                "opt_object": {
                    CONF_DEFAULT: {},
                    "selector": {"object": {}},
                },
            }
        },
        "action": Input("opt_actions"),
    }
    assert validate_safe_input_usages(safe_blueprint) == []

    # Empty default inputs explicitly passed
    empty_defaults = {"empty_inp": ""}
    node = {
        "variables": {"var1": Input("empty_inp")},
        CONF_DEFAULT: Input("empty_inp"),
        "target": {"entity_id": Input("empty_inp")},
        "service": Input("empty_inp"),
    }
    errors2 = validate_safe_input_usages(node, empty_defaults, "path.to.node")
    assert any("service" in err or "target" in err for err in errors2)


def test_check_ha_template_ast_compatibility(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test template AST compatibility checks."""
    env = TemplateEnvironment(hass)

    def check_template(template_str: str, target_env: TemplateEnvironment = env) -> list[str]:
        return check_ha_template_ast_compatibility(target_env.parse(template_str), target_env)

    # Valid template
    assert check_template("{{ 1 + 1 }}") == []

    # Math module usages: math.sin when sin is global
    err_math_sin = check_template("{{ math.sin(1) }}")
    assert any("use 'sin' instead of 'math.sin'" in err for err in err_math_sin)

    # Math module usages: math.cos when cos is filter
    cast(dict[str, Any], env.globals).pop("cos", None)
    env.filters["cos"] = lambda x: x
    err_math_cos = check_template("{{ math.cos(1.5) }}")
    assert any("| cos" in err for err in err_math_cos)

    # Math module usages: math.unknown
    err_math_unknown = check_template("{{ math.unknown(1) }}")
    assert any("'math' is not available" in err for err in err_math_unknown)

    # Bare math usage
    err_math_bare = check_template("{{ math }}")
    assert any("'math' is not available" in err for err in err_math_bare)

    # Unsupported math call registered as filter
    err_cos_call = check_template("{{ cos(1.5) }}")
    assert any("registered as a filter" in err for err in err_cos_call)

    # Unsupported math call not in filters
    err_fact = check_template("{{ factorial(5) }}")
    assert any("not registered as a global function" in err for err in err_fact)

    # Mutating method calls
    err_mut = check_template("{{ my_list.append(1) }}")
    assert any("mutating method" in err for err in err_mut)

    # Template imports: existing custom templates registered in loader (including subdirectories)
    assert isinstance(env.loader, HassLoader)
    monkeypatch.setitem(env.loader.sources, "test.jinja", "")
    monkeypatch.setitem(env.loader.sources, "macro.jinja", "")
    monkeypatch.setitem(env.loader.sources, "helpers.jinja", "")
    monkeypatch.setitem(env.loader.sources, "sub/helpers.jinja", "")
    assert check_template("{% import 'test.jinja' as t %}") == []
    assert check_template("{% from 'macro.jinja' import my_macro %}") == []
    assert check_template("{% from 'helpers.jinja' import helper %}") == []
    assert check_template("{% from 'sub/helpers.jinja' import helper %}") == []

    # Template imports: non-existent custom templates
    err_missing = check_template("{% import 'missing.jinja' as m %}")
    assert any(
        "custom template 'missing.jinja' does not exist in 'custom_templates'" in err
        for err in err_missing
    )

    # Template imports: custom_templates/ folder prefix is rejected
    err_prefix = check_template("{% from 'custom_templates/macro.jinja' import my_macro %}")
    assert any(
        "custom template 'custom_templates/macro.jinja' does not exist in 'custom_templates'" in err
        for err in err_prefix
    )

    # Template imports: missing template in subdirectory
    err_subdir = check_template("{% from 'sub/missing.jinja' import helper %}")
    assert any(
        "custom template 'sub/missing.jinja' does not exist in 'custom_templates'" in err
        for err in err_subdir
    )

    # Template imports: environment with no loader/hass skips existence check
    env_no_loader = TemplateEnvironment(None)
    assert check_template("{% import 'unknown.jinja' as u %}", env_no_loader) == []
    assert check_template("{% from 'sub/unknown.jinja' import u %}", env_no_loader) == []

    # Template imports: template discovered on disk via hass.config.path
    with (
        patch.object(hass.config, "path", return_value="/config/custom_templates"),
        patch("os.path.isdir", return_value=True),
        patch("pathlib.Path.rglob") as mock_rglob,
    ):
        mock_file = MagicMock()
        mock_file.is_file.return_value = True
        mock_file.stat.return_value.st_size = 100
        mock_file.relative_to.return_value = Path("sub/disk_macro.jinja")
        mock_rglob.return_value = [mock_file]
        env_disk = TemplateEnvironment(hass)
        env_disk.loader = HassLoader({})
        assert check_template("{% from 'sub/disk_macro.jinja' import disk_macro %}", env_disk) == []

    # Template imports: disallowed Python modules
    err_import_math = check_template("{% import 'math' as m %}")
    assert any("cannot import Python modules via 'math'" in err for err in err_import_math)

    err_from_os = check_template("{% from 'os' import path %}")
    assert any("cannot import Python modules via 'os'" in err for err in err_from_os)

    err_import_name = check_template("{% import math as m %}")
    assert any("cannot import Python modules via 'math'" in err for err in err_import_name)

    err_import_not_jinja = check_template("{% import 'custom_templates/bad_module' as b %}")
    assert any(
        "cannot import Python modules via 'custom_templates/bad_module'" in err
        for err in err_import_not_jinja
    )

    # Template imports: disallowed absolute paths and directory traversal
    err_import_abs = check_template("{% import '/tmp/evil.jinja' as e %}")
    assert any(
        "custom template '/tmp/evil.jinja' does not exist in 'custom_templates'" in err
        for err in err_import_abs
    )

    err_from_traversal = check_template("{% from '../../evil.jinja' import bad %}")
    assert any(
        "custom template '../../evil.jinja' does not exist in 'custom_templates'" in err
        for err in err_from_traversal
    )

    err_import_traversal_sub = check_template("{% import 'custom_templates/../evil.jinja' as e %}")
    assert any(
        "custom template 'custom_templates/../evil.jinja' does not exist in 'custom_templates'"
        in err
        for err in err_import_traversal_sub
    )

    # Environment with math in globals returns empty errors
    env_with_math = TemplateEnvironment(hass)
    cast(dict[str, Any], env_with_math.globals)["math"] = object()
    assert check_template("{{ math.something }}", env_with_math) == []

    # Scoped names: local assignment and for-loop unpacking
    assert (
        check_template(
            "{% for a, (b, c) in items %}{{ a }}{% endfor %}"
            "{% macro test_macro(m1, m2) %}{{ m1 }}{% endmacro %}"
        )
        == []
    )

    # Scoped math: macro parameter shadowing
    assert check_template("{% macro foo(math) %}{{ math.sin(1) }}{% endmacro %}") == []
    macro_shadow_mixed = "{% macro foo(math) %}{{ math.sin(1) }}{% endmacro %}{{ math.sin(1) }}"
    err_macro_mixed = check_template(macro_shadow_mixed)
    assert len(err_macro_mixed) == 1
    assert "line 1" in err_macro_mixed[0]

    # Scoped math: import alias
    assert check_template("{% import 'macro.jinja' as math %}{{ math.sin(1) }}") == []
    assert check_template("{% from 'macro.jinja' import my_macro as math %}{{ math.sin(1) }}") == []

    # Scoped math: for-loop target shadowing
    assert check_template("{% for math in items %}{{ math.sin(1) }}{% endfor %}") == []
    for_mixed = "{% for math in items %}{{ math.value }}{% endfor %}{{ math.sin(1) }}"
    err_for_mixed = check_template(for_mixed)
    assert len(err_for_mixed) == 1

    # Scoped math: with statement shadowing
    assert check_template("{% with math = 5 %}{{ math }}{% endwith %}") == []
    with_mixed = "{% with math = 5 %}{{ math }}{% endwith %}{{ math.sin(1) }}"
    err_with_mixed = check_template(with_mixed)
    assert len(err_with_mixed) == 1

    # Scoped math: set statement assignment
    assert check_template("{% set math = 5 %}{{ math }}") == []


def test_input_extraction_helpers():
    """Test input extraction and reference validation functions."""
    # Non-dict inputs
    assert extract_defined_inputs(None) == set()
    assert extract_inputs_with_default(None) == set()
    assert extract_mandatory_inputs(None) == set()
    assert extract_input_configs(None) == {}

    raw_inputs = {
        "mandatory_one": {"name": "Mandatory 1"},
        "defaulted_one": {"name": "Defaulted 1", "default": "value"},
        "section_a": {
            "name": "Section A",
            "input": {
                "nested_mandatory": {"name": "Nested Mandatory"},
                "nested_default": {"name": "Nested Default", "default": 10},
            },
        },
        "bare_key": None,
    }

    expected_defined = {
        "mandatory_one",
        "defaulted_one",
        "nested_mandatory",
        "nested_default",
        "bare_key",
    }
    assert extract_defined_inputs(raw_inputs) == expected_defined

    assert extract_inputs_with_default(raw_inputs) == {"defaulted_one", "nested_default"}
    assert extract_mandatory_inputs(raw_inputs) == {
        "mandatory_one",
        "nested_mandatory",
        "bare_key",
    }

    configs = extract_input_configs(raw_inputs)
    assert {"mandatory_one", "nested_mandatory", "bare_key"}.issubset(configs.keys())

    # Used inputs extraction
    data = {
        "action": [
            {"service": Input("mandatory_one")},
            {"target": {"entity_id": Input("nested_mandatory")}},
            "!input not_an_input_obj",
        ]
    }
    used = extract_used_inputs(data)
    assert "mandatory_one" in used
    assert "nested_mandatory" in used
    assert extract_used_inputs(None) == []

    # Reference validation
    valid_refs = validate_input_references(
        {"blueprint": {CONF_INPUT: raw_inputs}, "action": [Input("mandatory_one")]}
    )
    assert valid_refs is None

    invalid_refs = validate_input_references(
        {"blueprint": {CONF_INPUT: raw_inputs}, "action": [Input("non_existent_input")]}
    )
    assert invalid_refs is not None
    assert "non_existent_input" in invalid_refs

    # Missing blueprint mapping in validate_input_references
    assert validate_input_references({}) is None


def test_risk_detection_helpers():
    """Test risk detection helpers."""
    configs = {
        "automation.auto1": {"inp1": "val1", "inp2": "val2"},
        "automation.auto2": {"inp2": "val2"},
    }
    affected = get_affected_entities(configs, "inp1")
    assert affected == ["automation.auto1"]

    # is_input_mandatory
    assert is_input_mandatory("bad") is True
    assert is_input_mandatory({"default": 1}) is False
    assert is_input_mandatory({"mandatory": True}) is True
    assert is_input_mandatory({"name": "Req"}) is True

    # detect_new_mandatory_inputs
    old_inputs = {"inp1": {"default": 1}}
    new_inputs = {
        "inp1": {"default": 1},
        "new_mand": {"name": "New Mandatory"},
    }
    new_mand = detect_new_mandatory_inputs(old_inputs, new_inputs)
    assert len(new_mand) == 1
    assert new_mand[0]["type"] == BlueprintRiskType.NEW_MANDATORY
    assert new_mand[0]["args"]["input"] == "new_mand"

    # detect_missing_inputs
    new_schema = {
        "req_inp": {"mandatory": True},
        "defaultless_empty": {},
        "defaultless_named": {"name": "Required Name"},
        "optional_with_default": {"default": "some_value"},
        "optional_with_null_default": {"default": None},
        "explicit_optional": {"mandatory": False},
    }
    existing_configs = {"automation.my_auto": {"other": 1}}
    missing = detect_missing_inputs(new_schema, existing_configs)
    assert len(missing) == 3
    missing_inputs = [m["args"]["input"] for m in missing]
    assert missing_inputs == ["req_inp", "defaultless_empty", "defaultless_named"]
    assert all(m["type"] == BlueprintRiskType.MISSING_INPUT for m in missing)
    assert all(m["args"]["entity"] == "automation.my_auto" for m in missing)

    existing_configs_satisfied = {
        "automation.my_auto": {
            "req_inp": "val1",
            "defaultless_empty": "val2",
            "defaultless_named": "val3",
        }
    }
    assert detect_missing_inputs(new_schema, existing_configs_satisfied) == []

    # dedupe_risks
    risk_list: list[StructuredRisk] = [
        {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "a"}},
        {"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": "a"}},
        {"type": BlueprintRiskType.MISSING_INPUT, "args": {"entity": "e1"}},
    ]
    deduped = dedupe_risks(risk_list)
    assert len(deduped) == 2

    # dedupe_risks with malformed items
    assert dedupe_risks(cast(list[StructuredRisk], ["invalid"])) == []


def test_normalization_and_yaml_stabilization():
    """Test YAML normalization, schema lookup, and diffing."""
    content = "blueprint:\r\n  name: Test  \r\n\r\n"
    norm = normalize_content(content)
    assert norm.endswith("\n")
    assert not norm.endswith("\r\n")

    # Canonicalize source url
    url = "https://example.com/test.yaml#frag?q=1"
    assert canonicalize_source_url(url) == "https://example.com/test.yaml"
    assert canonicalize_source_url("") == ""

    # Schema lookup
    assert get_blueprint_schema("automation") is not None
    assert get_blueprint_schema("script") is not None
    assert get_blueprint_schema("template") is not None
    assert get_blueprint_schema("unknown_domain") is not None

    # coerce_empty_selectors
    sel_data = {"selector": {"entity": None}, "list": [{"selector": {"text": None}}]}
    coerce_empty_selectors(sel_data)
    assert sel_data["selector"]["entity"] == {}
    assert sel_data["list"][0]["selector"]["text"] == {}

    # hash_content
    raw = "blueprint:\n  name: Test\n"
    assert hash_content(raw, already_normalized=True) is not None
    assert hash_content(raw, source_url=None) is not None
    assert hash_content(raw, source_url="https://example.com/test.yaml") is not None

    # stabilize_yaml_structure
    orig = {"b": 2, "a": 1, "items": [{"x": 10}]}
    norm_dict = {"a": 1, "b": 2, "items": [{"x": 10}]}
    stabilized = stabilize_yaml_structure(orig, norm_dict)
    assert isinstance(stabilized, dict)
    assert list(stabilized.keys()) == ["b", "a", "items"]

    # extract_blueprint_text & get_blueprint_block
    bp_text = extract_blueprint_text("blueprint:\n  name: Test\naction: []\n")
    assert "name: Test" in bp_text
    assert extract_blueprint_text("not: valid: yaml: [") == "not: valid: yaml: ["

    block = get_blueprint_block("test.yaml", content="blueprint:\n  name: Test\naction: []\n")
    assert block is not None
    assert "name" in block
    assert get_blueprint_block("test.yaml", content="no_blueprint: true") is None
    assert get_blueprint_block("test.yaml", parsed_data={"blueprint": {"name": "Parsed"}}) == {
        "name": "Parsed"
    }


def test_read_and_diff(tmp_path: Path):
    """Test read_and_diff helper."""
    local_file = tmp_path / "local.yaml"
    local_file.write_text("blueprint:\n  name: Old\n", encoding="utf-8")
    remote_text = "blueprint:\n  name: New\n"
    url = "https://example.com/test.yaml"

    diff = read_and_diff(str(local_file), remote_text, url)
    assert diff != ""
    assert "Old" in diff
    assert "New" in diff


def test_ensure_source_url_and_cached():
    """Test ensure_source_url with custom normalize_fn and dump_fn."""
    # Non-string content
    assert ensure_source_url(123, "https://example.com") == ""

    # Non-string or empty URL
    assert ensure_source_url("blueprint:\n  name: Test\n", "") == "blueprint:\n  name: Test\n"
    assert ensure_source_url("blueprint:\n  name: Test\n", None) == "blueprint:\n  name: Test\n"

    # Valid source URL injection
    bp_yaml = "blueprint:\n  name: Test\n  domain: automation\naction: []\n"
    res = ensure_source_url(bp_yaml, "https://example.com/test.yaml")
    assert "source_url: https://example.com/test.yaml" in res

    # ensure_source_url_cached with unparseable content
    unparseable = "bad: yaml: [["
    assert ensure_source_url_cached(unparseable, "https://example.com") == unparseable

    # ensure_source_url_cached with missing blueprint key
    no_bp = "other_key: val\n"
    assert ensure_source_url_cached(no_bp, "https://example.com") == no_bp

    # ensure_source_url_cached with non-dict blueprint
    non_dict_bp = "blueprint: string_val\n"
    assert ensure_source_url_cached(non_dict_bp, "https://example.com") == non_dict_bp

    # Custom normalize_fn and dump_fn
    custom_norm = MagicMock(return_value="custom_normalized")
    custom_dump = MagicMock(return_value="custom_dumped")
    res_custom = ensure_source_url(
        bp_yaml,
        "https://example.com/test.yaml",
        normalize_fn=custom_norm,
        dump_fn=custom_dump,
    )
    assert res_custom == "custom_dumped"

    # Dump failure fallback
    failing_dump = MagicMock(side_effect=ValueError("dump fail"))
    res_fail = ensure_source_url(
        bp_yaml,
        "https://example.com/test.yaml",
        normalize_fn=custom_norm,
        dump_fn=failing_dump,
    )
    assert res_fail == "custom_normalized"

    # Idempotence: calling ensure_source_url repeatedly on its own output
    output1 = ensure_source_url(bp_yaml, "https://example.com/test.yaml")
    output2 = ensure_source_url(output1, "https://example.com/test.yaml")
    assert output1 == output2
    assert output2.count("source_url:") == 1

    # YAML normalization idempotence and hash_content consistency
    raw_messy = "blueprint:\r\n  name: Test  \r\n\r\n  domain: automation\r\n"
    norm1 = normalize_content(raw_messy)
    norm2 = normalize_content(norm1)
    assert norm1 == norm2

    h1 = hash_content(raw_messy)
    h2 = hash_content(norm1, already_normalized=True)
    assert h1 == h2


def test_get_selector_filter_paths_caching():
    """Verify get_selector_filter_paths returns cached paths without re-derivation."""
    invalidate_selector_filter_paths_cache()
    paths1 = get_selector_filter_paths()
    assert paths1 is not None
    quick_sig = get_cached_selector_registry_quick_sig()
    assert isinstance(quick_sig, SelectorRegistryQuickSig)

    with patch(
        "custom_components.blueprints_updater.blueprint_validation.derive_selector_filter_paths"
    ) as mock_derive:
        paths2 = get_selector_filter_paths()
        assert paths2 is paths1
        mock_derive.assert_not_called()

    invalidate_selector_filter_paths_cache()


def test_extract_custom_template_paths(tmp_path: Path):
    """Test extracting custom template paths with posix normalization and boundary conditions."""
    # env is None
    assert _extract_custom_template_paths(None) is None

    # Empty env without loader or hass
    mock_env = MagicMock()
    mock_env.loader = None
    mock_env.hass = None
    assert _extract_custom_template_paths(mock_env) is None

    # Loader with sources
    mock_loader = MagicMock()
    mock_loader.sources = {"my_macro.jinja": "content"}
    mock_env.loader = mock_loader
    assert _extract_custom_template_paths(mock_env) == {"my_macro.jinja"}

    # Loader with mapping
    mock_loader.sources = None
    mock_loader.mapping = {"nested/macro.jinja": "content"}
    assert _extract_custom_template_paths(mock_env) == {"nested/macro.jinja"}

    # Filesystem fallback with hass.config.path
    mock_env.loader = None
    templates_dir = tmp_path / "custom_templates"
    sub_dir = templates_dir / "subdir"
    sub_dir.mkdir(parents=True)
    valid_file = sub_dir / "nested_tmpl.jinja"
    valid_file.write_text("{% macro foo() %}bar{% endmacro %}")
    non_jinja_file = sub_dir / "ignored.txt"
    non_jinja_file.write_text("ignored")

    mock_hass = MagicMock()
    mock_hass.config.path.return_value = str(templates_dir)
    mock_env.hass = mock_hass

    paths = _extract_custom_template_paths(mock_env)
    assert paths == {"subdir/nested_tmpl.jinja"}

    # Exceeding MAX_CUSTOM_TEMPLATE_SIZE
    large_file = sub_dir / "large.jinja"
    large_file.write_text("a" * (MAX_CUSTOM_TEMPLATE_SIZE + 1))
    paths_with_large = _extract_custom_template_paths(mock_env)
    assert paths_with_large is not None
    assert "subdir/large.jinja" not in paths_with_large


def test_inspect_scoped_math_subtree():
    """Test _inspect_scoped_math directly on an AST node."""
    env = TemplateEnvironment(None)
    ast = env.parse("{{ math.sqrt(16) }}")
    errors: list[str] = []
    reported_lines: set[int] = set()
    _inspect_scoped_math(
        ast,
        env,
        current_scope=set(),
        reported_math_lines=reported_lines,
        errors=errors,
        math_globals_desc=" (available globals: sqrt)",
    )
    assert len(errors) == 1
    assert "math" in errors[0]


def test_get_ha_live_selectors():
    """Test retrieving live selectors from Home Assistant Core."""
    selectors = get_ha_live_selectors()
    assert isinstance(selectors, Mapping)
    assert len(selectors) > 0
    assert SelectorType.ENTITY in selectors
    assert SelectorType.TARGET in selectors
    assert SelectorType.NUMBER in selectors

    # Test SelectorType enum values match strings
    assert SelectorType.ACTION == "action"
    assert SelectorType.LOCATION == "location"
    assert SelectorType.MEDIA == "media"

    # Test with exception or None
    with patch("homeassistant.helpers.selector.SELECTORS", None):
        assert get_ha_live_selectors() == {}


def test_all_live_ha_selectors_registered_in_enum() -> None:
    """Verify that every live selector in Home Assistant Core is registered in SelectorType."""
    live_selectors = get_ha_live_selectors()
    assert len(live_selectors) > 0
    assert set(live_selectors).issubset(set(SelectorType))
    assert SelectorType.FILTER not in live_selectors


_MINIMAL_SELECTOR_CONFIGS: Final[dict[SelectorType, dict[str, object]]] = {
    SelectorType.ATTRIBUTE: {"entity_id": "light.dummy"},
    SelectorType.AUTOMATION_BEHAVIOR: {"mode": "trigger"},
    SelectorType.CHOOSE: {"choices": {"choice_a": {"selector": {"boolean": {}}}}},
    SelectorType.CONSTANT: {"value": "test_const"},
    SelectorType.DEVICE_CLASS: {"domain": "binary_sensor"},
    SelectorType.FILE: {"accept": ".txt"},
    SelectorType.NUMBER: {"min": 0, "max": 100},
    SelectorType.NUMERIC_THRESHOLD: {"mode": "is"},
    SelectorType.QR_CODE: {"data": "dummy_data"},
    SelectorType.SELECT: {"options": ["option1", "option2"]},
    SelectorType.STATE: {"entity_id": "light.dummy"},
}

_ALL_LIVE_HA_SELECTORS: Final[tuple[SelectorType, ...]] = tuple(
    s for s in SelectorType if s != SelectorType.FILTER
)


@pytest.mark.parametrize("selector_type", _ALL_LIVE_HA_SELECTORS)
def test_selector_dummy_value_validation(selector_type: SelectorType) -> None:
    """Verify that selector produces a valid dummy value satisfying live schema."""
    config = _MINIMAL_SELECTOR_CONFIGS.get(selector_type, {})
    sel_cfg: dict[str, object] = {selector_type.value: config}

    dummy = generate_dummy_input_value(sel_cfg)
    assert dummy is not None
    valid = _is_dummy_value_valid_for_selector(sel_cfg, dummy)
    assert valid is True


def test_generate_dummy_input_value_defensive_copying() -> None:
    """Verify generate_dummy_input_value returns defensive copies of mutable defaults."""
    action_dummy = generate_dummy_input_value({SelectorType.ACTION.value: {}})
    assert isinstance(action_dummy, list)
    action_dummy.append({"mutated": True})

    fresh_action = generate_dummy_input_value({SelectorType.ACTION.value: {}})
    assert fresh_action == []

    duration_dummy = generate_dummy_input_value({SelectorType.DURATION.value: {}})
    assert isinstance(duration_dummy, dict)
    duration_dummy["hours"] = 99

    fresh_duration = generate_dummy_input_value({SelectorType.DURATION.value: {}})
    assert isinstance(fresh_duration, dict)
    assert fresh_duration["hours"] == 0
