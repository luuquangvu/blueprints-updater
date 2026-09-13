"""Blueprint validation, template inspection, schema fingerprinting, and normalization."""

from __future__ import annotations

import contextlib
import copy
import difflib
import functools
import hashlib
import logging
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, NamedTuple, TypedDict

import homeassistant.components.template.config as template_config
import orjson

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.automation.const import CONF_TRIGGER_VARIABLES
from homeassistant.components.blueprint.const import CONF_BLUEPRINT, CONF_INPUT
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_FLOOR_ID,
    ATTR_LABEL_ID,
    CONF_ACTION,
    CONF_CONDITION,
    CONF_CONDITIONS,
    CONF_DEFAULT,
    CONF_DOMAIN,
    CONF_IF,
    CONF_SERVICE,
    CONF_TARGET,
    CONF_TRIGGER,
    CONF_TRIGGERS,
    CONF_UNTIL,
    CONF_VARIABLES,
    CONF_WAIT_FOR_TRIGGER,
    CONF_WHILE,
)

if TYPE_CHECKING:
    from homeassistant.const import ATTR_CONFIG_ENTRY_ID
else:
    try:
        from homeassistant.const import ATTR_CONFIG_ENTRY_ID
    except ImportError:
        ATTR_CONFIG_ENTRY_ID = "config_entry_id"
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector as ha_selector
from homeassistant.helpers.template import MAX_CUSTOM_TEMPLATE_SIZE, TemplateEnvironment
from homeassistant.util import yaml as yaml_util
from homeassistant.util.yaml.objects import Input
from jinja2 import nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .const import (
    BlueprintRiskType,
    FunctionalDomain,
)
from .providers import registry
from .utils import redact_url

_LOGGER = logging.getLogger(__name__)

_raw_template_schema: object = getattr(template_config, "BLUEPRINT_SCHEMA", None)
TEMPLATE_BLUEPRINT_SCHEMA: vol.Schema | None = (
    _raw_template_schema if isinstance(_raw_template_schema, vol.Schema) else None
)


class SelectorType(StrEnum):
    """Home Assistant Core selector type identifiers."""

    ACTION = "action"
    ADDON = "addon"
    APP = "app"
    AREA = "area"
    ASSIST_PIPELINE = "assist_pipeline"
    ATTRIBUTE = "attribute"
    AUTOMATION_BEHAVIOR = "automation_behavior"
    BACKUP_LOCATION = "backup_location"
    BOOLEAN = "boolean"
    CHOOSE = "choose"
    COLOR_RGB = "color_rgb"
    COLOR_TEMP = "color_temp"
    CONDITION = "condition"
    CONFIG_ENTRY = "config_entry"
    CONSTANT = "constant"
    CONVERSATION_AGENT = "conversation_agent"
    COUNTRY = "country"
    DATE = "date"
    DATETIME = "datetime"
    DEVICE = "device"
    DEVICE_CLASS = "device_class"
    DURATION = "duration"
    ENTITY = "entity"
    FILE = "file"
    FILTER = "filter"
    FLOOR = "floor"
    ICON = "icon"
    LABEL = "label"
    LANGUAGE = "language"
    LOCATION = "location"
    MEDIA = "media"
    NUMBER = "number"
    NUMERIC_THRESHOLD = "numeric_threshold"
    OBJECT = "object"
    QR_CODE = "qr_code"
    SELECT = "select"
    SERIAL_PORT = "serial_port"
    STATE = "state"
    STATISTIC = "statistic"
    TARGET = "target"
    TEMPLATE = "template"
    TEXT = "text"
    THEME = "theme"
    TIME = "time"
    TRIGGER = "trigger"


_KNOWN_SELECTOR_TYPES: Final[frozenset[str]] = frozenset(s.value for s in SelectorType)


type SelectorRegistryFingerprint = tuple[tuple[str, str, tuple[object, ...]], ...]

DEFAULT_SELECTOR_FILTER_PATHS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        (SelectorType.ENTITY, SelectorType.FILTER),
        (SelectorType.DEVICE, SelectorType.FILTER),
        (SelectorType.DEVICE, SelectorType.ENTITY),
        (SelectorType.TARGET, SelectorType.ENTITY),
        (SelectorType.TARGET, SelectorType.DEVICE),
        (SelectorType.AREA, SelectorType.ENTITY),
        (SelectorType.AREA, SelectorType.DEVICE),
        (SelectorType.FLOOR, SelectorType.ENTITY),
        (SelectorType.FLOOR, SelectorType.DEVICE),
        (SelectorType.NUMERIC_THRESHOLD, SelectorType.ENTITY),
    }
)

JSONDict = Mapping[str, "JSONValue"]
JSONList = Sequence["JSONValue"]
JSONValue = None | bool | int | float | str | JSONDict | JSONList


class StructuredRisk(TypedDict):
    """Structured breaking change risk.

    The ``args`` field must be JSON-serializable, as it is used for
    deduplication and logging. Typical shapes include e.g.
    ``{"input": "<string>"}`` or ``{"entity": "<entity_id>", "error": "<string>"}``.
    """

    type: BlueprintRiskType
    args: JSONDict


_MATH_MODULE: Final[str] = "math"
_MATH_UNAVAILABLE_MSG: Final[str] = (
    f"'{_MATH_MODULE}' is not available in Home Assistant templates."
)
_AST_CTX_LOAD: Final[str] = "load"
_ALLOWED_TEMPLATE_EXTENSION: Final[str] = ".jinja"
_CUSTOM_TEMPLATES_FOLDER: Final[str] = "custom_templates"
_DEFAULT_DUMMY_VALUE: Final[str] = "test.dummy"
_DEFAULT_SELECT_OPTION: Final[str] = "option1"
_CONF_SELECTOR: Final[str] = "selector"
_CONF_SOURCE_URL: Final[str] = "source_url"
_CONF_MANDATORY: Final[str] = "mandatory"
_UTF8_ENCODING: Final[str] = "utf-8"
_ROOT_PATH: Final[str] = "root"


_PYTHON_MATH_NAMES: Final[frozenset[str]] = frozenset(
    name for name in dir(math) if not name.startswith("_") and callable(getattr(math, name, None))
)


def _get_ha_mutable_method_names() -> frozenset[str]:
    """Dynamically determine mutating collection methods blocked by Home Assistant's sandbox.

    Returns:
        Frozenset of blocked mutating method names.

    """
    blocked_names: set[str] = set()
    env = ImmutableSandboxedEnvironment()

    test_targets: list[object] = [[], {}, set()]
    for target in test_targets:
        for attr in dir(target):
            if attr.startswith("_"):
                continue
            val = getattr(target, attr, None)
            if not callable(val):
                continue
            try:
                if not env.is_safe_attribute(target, attr, val):
                    blocked_names.add(attr)
            except Exception:
                blocked_names.add(attr)

    blocked_names.update({"append", "extend", "insert", "pop", "remove", "clear", "update"})
    return frozenset(blocked_names)


_HA_MUTABLE_METHOD_NAMES: Final[frozenset[str]] = _get_ha_mutable_method_names()


def _fingerprint_schema_key(sk: object) -> tuple[str, str, tuple[object, ...] | None]:
    """Deterministically fingerprint a schema key including marker metadata."""
    if isinstance(sk, (vol.Optional, vol.Required, vol.Marker)):
        marker_name = type(sk).__name__
        default_val = getattr(sk, "default", vol.UNDEFINED)
        default_fp = (
            compute_selector_schema_fingerprint(default_val)
            if default_val is not vol.UNDEFINED
            else None
        )
        return (marker_name, str(sk.schema), default_fp)
    return (type(sk).__name__, str(sk), None)


def _fingerprint_dict_entries(
    items: Mapping[object, object],
) -> tuple[tuple[tuple[str, str, tuple[object, ...] | None], tuple[object, ...]], ...]:
    """Deterministically fingerprint mapping entries.

    Args:
        items: Mapping of schema keys to child schemas.

    Returns:
        Sorted tuple of key-value fingerprint pairs.

    """
    entries = [
        (_fingerprint_schema_key(sk), compute_selector_schema_fingerprint(sv))
        for sk, sv in items.items()
    ]
    return tuple(sorted(entries, key=lambda x: str(x[0])))


def _fingerprint_vol_validator(
    schema_obj: object, type_name: str
) -> tuple[str, str, tuple[object, ...]]:
    """Fingerprint voluptuous validator instances like Coerce, In, Range, Length.

    Args:
        schema_obj: Voluptuous validator instance.
        type_name: Class name of the validator.

    Returns:
        Fingerprint tuple identifying validator and its parameters.

    """
    extra_attrs = tuple(
        (attr, compute_selector_schema_fingerprint(getattr(schema_obj, attr)))
        for attr in ("type", "container", "min", "max")
        if hasattr(schema_obj, attr)
    )
    return ("vol_validator", type_name, extra_attrs)


def _fingerprint_partial(
    schema_obj: functools.partial[object],
) -> tuple[
    str,
    tuple[object, ...],
    tuple[tuple[object, ...], tuple[tuple[str, tuple[object, ...]], ...]],
]:
    """Fingerprint functools.partial instance.

    Args:
        schema_obj: Partial function instance.

    Returns:
        Fingerprint tuple for function, positional arguments, and keywords.

    """
    args_fps = tuple(compute_selector_schema_fingerprint(a) for a in schema_obj.args)
    kwargs_fps = tuple(
        sorted(
            (
                (str(k), compute_selector_schema_fingerprint(v))
                for k, v in (schema_obj.keywords or {}).items()
            ),
            key=lambda x: x[0],
        )
    )
    return (
        "partial",
        compute_selector_schema_fingerprint(schema_obj.func),
        (args_fps, kwargs_fps),
    )


def _fingerprint_opaque_object(schema_obj: object) -> tuple[str, str, tuple[object, ...]]:
    """Fingerprint arbitrary non-callable objects via instance dictionary.

    Args:
        schema_obj: Arbitrary object to fingerprint.

    Returns:
        Fingerprint tuple containing class name and non-private instance attributes.

    """
    obj_dict = getattr(schema_obj, "__dict__", None)
    dict_items = obj_dict.items() if isinstance(obj_dict, dict) else []
    extra_state = tuple(
        (str(k), compute_selector_schema_fingerprint(v))
        for k, v in sorted(dict_items, key=lambda x: str(x[0]))
        if not str(k).startswith("_")
    )
    qualname = getattr(type(schema_obj), "__qualname__", getattr(type(schema_obj), "__name__", ""))
    module = getattr(type(schema_obj), "__module__", "")
    return ("opaque_obj", f"{module}.{qualname}", extra_state)


def compute_selector_schema_fingerprint(schema_obj: object) -> tuple[object, ...]:
    """Deterministically compute content-based fingerprint for a selector voluptuous schema."""
    if isinstance(schema_obj, vol.Schema) and isinstance(schema_obj.schema, dict):
        return ("vol.Schema", len(schema_obj.schema), _fingerprint_dict_entries(schema_obj.schema))
    if isinstance(schema_obj, dict):
        return ("dict", len(schema_obj), _fingerprint_dict_entries(schema_obj))
    if isinstance(schema_obj, (vol.All, vol.Any)):
        type_name = "vol.All" if isinstance(schema_obj, vol.All) else "vol.Any"
        sub_fps = tuple(compute_selector_schema_fingerprint(v) for v in schema_obj.validators)
        return (type_name, len(sub_fps), sub_fps)
    if isinstance(schema_obj, (set, frozenset)):
        sub_fps = tuple(
            sorted((compute_selector_schema_fingerprint(v) for v in schema_obj), key=str)
        )
        return (type(schema_obj).__name__, len(sub_fps), sub_fps)
    if isinstance(schema_obj, (list, tuple)):
        sub_fps = tuple(compute_selector_schema_fingerprint(v) for v in schema_obj)
        return (type(schema_obj).__name__, len(sub_fps), sub_fps)
    if isinstance(schema_obj, functools.partial):
        return _fingerprint_partial(schema_obj)
    if isinstance(schema_obj, (vol.Coerce, vol.In, vol.Range, vol.Length)):
        return _fingerprint_vol_validator(schema_obj, type(schema_obj).__name__)
    if isinstance(schema_obj, (str, int, float, bool, type(None))):
        return ("literal", type(schema_obj).__name__, schema_obj)
    if isinstance(schema_obj, type):
        qualname = getattr(schema_obj, "__qualname__", getattr(schema_obj, "__name__", ""))
        module = getattr(schema_obj, "__module__", "")
        return ("type", f"{module}.{qualname}", ())
    if callable(schema_obj):
        return _fingerprint_callable_state(schema_obj)
    return _fingerprint_opaque_object(schema_obj)


def _fingerprint_callable_state(schema_obj: object) -> tuple[str, str, tuple[object, ...]]:
    """Deterministically fingerprint callable name, closure state, and instance variables."""
    qualname = (
        getattr(schema_obj, "__qualname__", getattr(schema_obj, "__name__", ""))
        or type(schema_obj).__name__
    )
    module = getattr(schema_obj, "__module__", "")
    extra_state: list[tuple[str, tuple[object, ...]]] = []
    if defaults := getattr(schema_obj, "__defaults__", None):
        extra_state.append(("defaults", compute_selector_schema_fingerprint(defaults)))
    if kwdefaults := getattr(schema_obj, "__kwdefaults__", None):
        extra_state.append(("kwdefaults", compute_selector_schema_fingerprint(kwdefaults)))
    if closure := getattr(schema_obj, "__closure__", None):
        cell_contents: list[tuple[object, ...]] = []
        for cell in closure:
            try:
                cell_contents.append(compute_selector_schema_fingerprint(cell.cell_contents))
            except Exception:
                cell_contents.append(("unrepr_cell",))
        extra_state.append(("closure", tuple(cell_contents)))
    if hasattr(schema_obj, "__dict__") and isinstance(schema_obj.__dict__, dict):
        extra_state.extend(
            (str(k), compute_selector_schema_fingerprint(v))
            for k, v in sorted(schema_obj.__dict__.items(), key=lambda x: str(x[0]))
            if not str(k).startswith("_")
        )
    return ("callable", f"{module}.{qualname}", tuple(extra_state))


def _is_dict_validator(val: object) -> bool:
    """Check whether a schema validator matches a dictionary structure."""
    if isinstance(val, vol.Schema):
        return _is_dict_validator(val.schema)
    if isinstance(val, dict):
        return True
    if isinstance(val, (vol.All, vol.Any)):
        return any(_is_dict_validator(v) for v in val.validators)
    return False


def _has_dict_list_expansion(val: object) -> bool:
    """Detect whether a schema validator coerces single dictionary to list of dictionaries."""
    if isinstance(val, vol.All):
        has_ensure_list = any(
            v is cv.ensure_list
            or getattr(v, "__name__", "") == "ensure_list"
            or (hasattr(v, "__qualname__") and "ensure_list" in v.__qualname__)
            for v in val.validators
        )
        has_list_of_dicts = any(
            isinstance(v, (list, tuple)) and len(v) == 1 and _is_dict_validator(v[0])
            for v in val.validators
        )
        if has_ensure_list and has_list_of_dicts:
            return True
        return any(_has_dict_list_expansion(v) for v in val.validators)

    if isinstance(val, vol.Any):
        return any(_has_dict_list_expansion(v) for v in val.validators)

    if isinstance(val, vol.Schema):
        return _has_dict_list_expansion(val.schema)

    return False


def _extract_schema_dicts(config_schema: object) -> list[dict[object, object]]:
    """Extract all underlying schema mappings from a voluptuous schema object."""
    if isinstance(config_schema, vol.Schema):
        return _extract_schema_dicts(config_schema.schema)
    if isinstance(config_schema, dict):
        return [config_schema]
    if isinstance(config_schema, (vol.All, vol.Any)):
        dicts: list[dict[object, object]] = []
        for v in config_schema.validators:
            dicts.extend(_extract_schema_dicts(v))
        return dicts
    return []


def _collect_selector_filter_paths(
    current_prefix: tuple[str, ...],
    schema_obj: object,
    discovered: set[tuple[str, ...]],
) -> None:
    """Recursively collect filter path prefixes matching list expansion schemas."""
    schema_dicts = _extract_schema_dicts(schema_obj)
    if not schema_dicts:
        return

    for schema_dict in schema_dicts:
        for key, val in schema_dict.items():
            key_name = (
                key.schema if isinstance(key, (vol.Optional, vol.Required, vol.Marker)) else key
            )
            if not isinstance(key_name, str):
                continue

            new_prefix = (*current_prefix, key_name)
            if _has_dict_list_expansion(val):
                discovered.add(new_prefix)

            _collect_selector_filter_paths(new_prefix, val, discovered)


def compute_selector_registry_fingerprint(
    selectors_registry: object,
) -> SelectorRegistryFingerprint | None:
    """Compute a recursive content fingerprint of selector classes and schemas in the registry."""
    if not isinstance(selectors_registry, Mapping):
        return None
    try:
        registry_items = list(selectors_registry.items())
    except Exception:
        _LOGGER.debug("Could not iterate Home Assistant selector registry items", exc_info=True)
        return None

    items: list[tuple[str, str, tuple[object, ...]]] = []
    for k, v in registry_items:
        try:
            config_schema = getattr(v, "CONFIG_SCHEMA", None)
            schema_fp = (
                compute_selector_schema_fingerprint(config_schema)
                if config_schema is not None
                else ()
            )
            cls_name = getattr(v, "__qualname__", getattr(v, "__name__", repr(v)))
        except Exception:
            cls_name = repr(v)
            schema_fp = ()
        items.append((str(k), cls_name, schema_fp))
    return tuple(sorted(items, key=lambda item: item[0]))


def derive_selector_filter_paths(
    selectors_registry: object | None,
    default_paths: frozenset[tuple[str, ...]] = DEFAULT_SELECTOR_FILTER_PATHS,
) -> frozenset[tuple[str, ...]]:
    """Dynamically inspect registered Home Assistant selectors to discover filter paths.

    Args:
        selectors_registry: Registry mapping selector types to selector classes.
        default_paths: Baseline fallback paths used if the registry is unavailable.

    Returns:
        Frozenset of selector filter path tuples discovered from the schemas.

    """
    discovered: set[tuple[str, ...]] = set()

    if isinstance(selectors_registry, Mapping) and selectors_registry:
        try:
            registry_items = list(selectors_registry.items())
        except Exception:
            _LOGGER.warning(
                "Could not iterate Home Assistant selector registry items; "
                "falling back to default paths",
                exc_info=True,
            )
            registry_items = []

        for selector_type, selector_cls in registry_items:
            try:
                config_schema = getattr(selector_cls, "CONFIG_SCHEMA", None)
                if config_schema is None:
                    continue

                _collect_selector_filter_paths((str(selector_type),), config_schema, discovered)
            except Exception:
                _LOGGER.warning(
                    "Failed to inspect selector schema for %s",
                    selector_type,
                    exc_info=True,
                )

        if registry_items and not discovered:
            _LOGGER.warning(
                "Selector dynamic discovery produced no paths from %d registered selectors; "
                "check schema introspection compatibility",
                len(registry_items),
            )
    else:
        _LOGGER.warning(
            "Home Assistant selector registry is unavailable or empty; "
            "falling back to default paths"
        )

    if default_paths and not default_paths.issubset(discovered):
        discovered.update(default_paths)

    return frozenset(discovered)


class SelectorRegistryQuickSig(NamedTuple):
    """Fast shallow signature for Home Assistant selector registry."""

    length: int
    items: tuple[tuple[str, int, int], ...]


_cached_selector_filter_paths: frozenset[tuple[str, ...]] | None = None
_cached_selector_registry_fingerprint: SelectorRegistryFingerprint | None = None
_cached_selector_registry_quick_sig: SelectorRegistryQuickSig | None = None


def invalidate_selector_filter_paths_cache() -> None:
    """Invalidate the cached selector filter paths and registry fingerprint."""
    global \
        _cached_selector_filter_paths, \
        _cached_selector_registry_fingerprint, \
        _cached_selector_registry_quick_sig
    _cached_selector_filter_paths = None
    _cached_selector_registry_fingerprint = None
    _cached_selector_registry_quick_sig = None


def get_cached_selector_registry_fingerprint() -> SelectorRegistryFingerprint | None:
    """Get the cached selector registry fingerprint.

    Returns:
        The cached registry fingerprint tuple or None if uninitialized.

    """
    return _cached_selector_registry_fingerprint


def get_cached_selector_registry_quick_sig() -> SelectorRegistryQuickSig | None:
    """Get the cached selector registry quick signature.

    Returns:
        The cached registry quick signature tuple or None if uninitialized.

    """
    return _cached_selector_registry_quick_sig


def set_cached_selector_filter_paths(
    paths: frozenset[tuple[str, ...]] | None,
    fingerprint: SelectorRegistryFingerprint | None = None,
    quick_sig: SelectorRegistryQuickSig | None = None,
) -> None:
    """Set the cached selector filter paths and registry fingerprint.

    Args:
        paths: Filter paths to store in the cache.
        fingerprint: Registry fingerprint to store in the cache.
        quick_sig: Registry quick signature to store in the cache.

    """
    global \
        _cached_selector_filter_paths, \
        _cached_selector_registry_fingerprint, \
        _cached_selector_registry_quick_sig
    _cached_selector_filter_paths = paths
    _cached_selector_registry_fingerprint = fingerprint
    _cached_selector_registry_quick_sig = quick_sig


def _get_registry_quick_signature(
    selectors_registry: object,
) -> SelectorRegistryQuickSig | None:
    """Compute a fast shallow signature of the selector registry mapping.

    Args:
        selectors_registry: Registry mapping selector types to selector classes.

    Returns:
        SelectorRegistryQuickSig or None if invalid.

    """
    if not isinstance(selectors_registry, Mapping):
        return None
    try:
        sig_items = tuple(
            (str(k), id(v), id(getattr(v, "CONFIG_SCHEMA", None)))
            for k, v in sorted(selectors_registry.items(), key=lambda item: str(item[0]))
        )
        return SelectorRegistryQuickSig(length=len(selectors_registry), items=sig_items)
    except Exception:
        return None


def get_selector_filter_paths(*, force_refresh: bool = False) -> frozenset[tuple[str, ...]]:
    """Get the cached or dynamically derived set of selector filter paths from HA Core.

    Args:
        force_refresh: Whether to ignore cached paths and re-derive from the registry.

    Returns:
        A frozenset of tuple paths identifying selector fields that expand to lists.

    """
    global \
        _cached_selector_filter_paths, \
        _cached_selector_registry_fingerprint, \
        _cached_selector_registry_quick_sig

    try:
        from homeassistant.helpers import selector as ha_selector

        selectors_registry = getattr(ha_selector, "SELECTORS", None)
    except Exception:
        _LOGGER.warning("Could not import Home Assistant selector registry", exc_info=True)
        selectors_registry = None

    current_fingerprint = compute_selector_registry_fingerprint(selectors_registry)

    if (
        force_refresh
        or _cached_selector_filter_paths is None
        or current_fingerprint != _cached_selector_registry_fingerprint
    ):
        _cached_selector_filter_paths = derive_selector_filter_paths(
            selectors_registry, DEFAULT_SELECTOR_FILTER_PATHS
        )
        _cached_selector_registry_fingerprint = current_fingerprint

    _cached_selector_registry_quick_sig = _get_registry_quick_signature(selectors_registry)
    return _cached_selector_filter_paths


def _get_ha_target_field_keys() -> frozenset[str]:
    """Dynamically extract valid target keys from Home Assistant Core.

    Returns:
        Frozenset of field names recognized as target inputs.

    """
    cv_target = getattr(cv, "TARGET_SERVICE_FIELDS", None)
    if isinstance(cv_target, (set, frozenset, list, tuple)):
        return frozenset(str(k) for k in cv_target)
    if isinstance(cv_target, Mapping):
        return frozenset(str(k) for k in cv_target)

    target_sel = getattr(ha_selector, "TargetSelector", None)
    cfg_schema = getattr(target_sel, "CONFIG_SCHEMA", None)
    if isinstance(cfg_schema, vol.Schema) and isinstance(cfg_schema.schema, Mapping):
        keys = set()
        for k in cfg_schema.schema:
            k_name = getattr(k, "schema", k)
            if isinstance(k_name, str):
                keys.add(k_name)
        if keys:
            return frozenset(keys)

    return frozenset(
        {
            ATTR_ENTITY_ID,
            ATTR_DEVICE_ID,
            ATTR_AREA_ID,
            ATTR_FLOOR_ID,
            ATTR_LABEL_ID,
        }
    )


def _multi_or_single(sub_cfg: object, dummy: str) -> list[str] | str:
    """Generate list of dummy strings or single string based on 'multiple' flag.

    Args:
        sub_cfg: Selector configuration mapping.
        dummy: Dummy string value.

    Returns:
        List containing dummy string if multiple=True, otherwise dummy string.

    """
    if isinstance(sub_cfg, Mapping) and sub_cfg.get("multiple"):
        return [dummy]
    return dummy


_ID_DUMMY_DEFAULTS: Final[dict[str, str]] = {
    SelectorType.DEVICE: f"dummy_{ATTR_DEVICE_ID}",
    SelectorType.AREA: f"dummy_{ATTR_AREA_ID}",
    SelectorType.FLOOR: f"dummy_{ATTR_FLOOR_ID}",
    SelectorType.LABEL: f"dummy_{ATTR_LABEL_ID}",
    SelectorType.CONFIG_ENTRY: f"dummy_{ATTR_CONFIG_ENTRY_ID}",
}

_SIMPLE_DUMMY_SELECTORS: Final[dict[str, object]] = {
    SelectorType.ACTION: [],
    SelectorType.ADDON: "core_ssh",
    SelectorType.APP: "dummy",
    SelectorType.ASSIST_PIPELINE: "preferred",
    SelectorType.ATTRIBUTE: "state",
    SelectorType.AUTOMATION_BEHAVIOR: "all",
    SelectorType.BACKUP_LOCATION: "/backup",
    SelectorType.BOOLEAN: False,
    SelectorType.COLOR_RGB: [255, 255, 255],
    SelectorType.CONDITION: [
        {CONF_CONDITION: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE, "state": "on"}
    ],
    SelectorType.CONVERSATION_AGENT: "homeassistant",
    SelectorType.DATE: "2026-01-01",
    SelectorType.DATETIME: "2026-01-01 00:00:00",
    SelectorType.DURATION: {"hours": 0, "minutes": 0, "seconds": 0},
    SelectorType.FILE: "00000000-0000-0000-0000-000000000000",
    SelectorType.ICON: "mdi:home",
    SelectorType.LOCATION: {"latitude": 0.0, "longitude": 0.0},
    SelectorType.NUMERIC_THRESHOLD: {"type": "above", "value": {"number": 0.0}},
    SelectorType.QR_CODE: "dummy",
    SelectorType.SERIAL_PORT: "/dev/ttyUSB0",
    SelectorType.STATE: "on",
    SelectorType.STATISTIC: "sensor.dummy",
    SelectorType.TEMPLATE: "",
    SelectorType.TEXT: "",
    SelectorType.THEME: "default",
    SelectorType.TIME: "00:00:00",
    SelectorType.TRIGGER: [{CONF_TRIGGER: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE}],
}


def _dummy_choose_value(sub_cfg: object) -> object:
    """Derive dummy value for a choose selector from its configured choices.

    Args:
        sub_cfg: Choose selector configuration mapping.

    Returns:
        Mapping or value satisfying the choose selector schema.

    """
    if isinstance(sub_cfg, Mapping):
        choices = sub_cfg.get("choices")
        if isinstance(choices, Mapping) and choices:
            first_key = next(iter(choices.keys()))
            first_choice = choices[first_key]
            if isinstance(first_choice, Mapping):
                sub_sel = first_choice.get("selector")
                if isinstance(sub_sel, Mapping):
                    sub_dummy = generate_dummy_input_value(sub_sel)
                    return {"active_choice": first_key, first_key: sub_dummy}
    return {}


def _dummy_device_class_value(sub_cfg: object) -> object:
    """Derive dummy value for a device_class selector honoring domain and multiple flag.

    Args:
        sub_cfg: Device class selector configuration mapping.

    Returns:
        Device class string or list of strings satisfying schema constraints.

    """
    dummy = "battery"
    if isinstance(sub_cfg, Mapping):
        domain = sub_cfg.get("domain")
        if domain == "switch":
            dummy = "switch"
        elif domain == "cover":
            dummy = "door"
        elif domain == "valve":
            dummy = "water"
        elif domain == "update":
            dummy = "firmware"
    return _multi_or_single(sub_cfg, dummy)


def _dummy_object_value(sub_cfg: object) -> object:
    """Derive dummy value for an object selector honoring configured fields and multiple flag.

    Args:
        sub_cfg: Object selector configuration mapping.

    Returns:
        Dictionary or list of dictionaries satisfying schema constraints.

    """
    obj: dict[str, object] = {}
    if isinstance(sub_cfg, Mapping):
        fields = sub_cfg.get("fields")
        if isinstance(fields, Mapping):
            for field_name, field_info in fields.items():
                if isinstance(field_info, Mapping) and field_info.get("required"):
                    field_sel = field_info.get("selector")
                    if isinstance(field_sel, Mapping):
                        obj[str(field_name)] = generate_dummy_input_value(field_sel)
                    else:
                        obj[str(field_name)] = "dummy"
        if sub_cfg.get("multiple"):
            return [obj]
    return obj


def _dummy_number_value(sub_cfg: object) -> float | int:
    """Derive dummy value for a number selector honoring bounds.

    Args:
        sub_cfg: Number selector configuration mapping.

    Returns:
        Numeric dummy value satisfying schema constraints.

    """
    if not isinstance(sub_cfg, Mapping):
        return 0

    if "min" in sub_cfg:
        try:
            return float(sub_cfg["min"])
        except (ValueError, TypeError):
            return 0
    if "max" in sub_cfg:
        try:
            max_val = float(sub_cfg["max"])
            return min(max_val, 0)
        except (ValueError, TypeError):
            return 0
    return 0


def _dummy_select_value(sub_cfg: object) -> object:
    """Derive dummy value for a select selector from its option configuration.

    Args:
        sub_cfg: Select selector configuration mapping.

    Returns:
        Selected dummy option scalar or list.

    """
    if isinstance(sub_cfg, Mapping):
        options = sub_cfg.get("options")
        if (
            isinstance(options, Sequence)
            and not isinstance(options, (str, bytes, bytearray))
            and options
        ):
            first = options[0]
            val = first.get("value", first) if isinstance(first, Mapping) else first
            return [val] if sub_cfg.get("multiple") else val
    return _multi_or_single(sub_cfg, _DEFAULT_SELECT_OPTION)


def _dummy_color_temp_value(sub_cfg: object) -> int:
    """Derive dummy value for a color_temp selector honoring unit and bounds.

    Args:
        sub_cfg: Color temp selector configuration mapping.

    Returns:
        Integer color temperature in mireds or Kelvin satisfying schema constraints.

    """
    if not isinstance(sub_cfg, Mapping):
        return 300

    is_kelvin = sub_cfg.get("unit") == "kelvin"
    default_val = 3000 if is_kelvin else 300
    min_val = sub_cfg.get("min")
    max_val = sub_cfg.get("max")
    if min_val is not None:
        with contextlib.suppress(ValueError, TypeError):
            default_val = max(default_val, int(min_val))
    if max_val is not None:
        with contextlib.suppress(ValueError, TypeError):
            default_val = min(default_val, int(max_val))
    return default_val


def _dummy_country_value(sub_cfg: object) -> list[str] | str:
    """Derive dummy value for a country selector honoring options and multiple flag.

    Args:
        sub_cfg: Country selector configuration mapping.

    Returns:
        Two-letter country code string or list of codes satisfying schema constraints.

    """
    country = "US"
    if isinstance(sub_cfg, Mapping):
        countries = sub_cfg.get("countries")
        if (
            isinstance(countries, Sequence)
            and not isinstance(countries, (str, bytes, bytearray))
            and countries
        ):
            first = countries[0]
            if isinstance(first, str) and len(first) == 2:
                country = first.upper()
    return _multi_or_single(sub_cfg, country)


def _dummy_language_value(sub_cfg: object) -> list[str] | str:
    """Derive dummy value for a language selector honoring options and multiple flag.

    Args:
        sub_cfg: Language selector configuration mapping.

    Returns:
        Language code string or list of language codes satisfying schema constraints.

    """
    lang = "en"
    if isinstance(sub_cfg, Mapping):
        languages = sub_cfg.get("languages")
        if (
            isinstance(languages, Sequence)
            and not isinstance(languages, (str, bytes, bytearray))
            and languages
        ):
            first = languages[0]
            if isinstance(first, str):
                lang = first
    return _multi_or_single(sub_cfg, lang)


def _dummy_media_value(sub_cfg: object) -> object:
    """Derive dummy value for a media selector honoring accept and multiple constraints.

    Args:
        sub_cfg: Media selector configuration mapping.

    Returns:
        Dictionary or list of dictionaries satisfying media selector schema.

    """
    has_accept = isinstance(sub_cfg, Mapping) and "accept" in sub_cfg
    dummy_item: dict[str, str] = {
        "media_content_id": "dummy",
        "media_content_type": "dummy",
    }
    if not has_accept:
        dummy_item["entity_id"] = "media_player.dummy"

    if isinstance(sub_cfg, Mapping) and sub_cfg.get("multiple"):
        return [dummy_item]
    return dummy_item


def _dummy_entity_value(sub_cfg: object) -> list[str] | str:
    """Derive dummy entity ID honoring domain filter and multiple flag.

    Args:
        sub_cfg: Entity selector configuration mapping.

    Returns:
        Entity ID string or list of entity ID strings satisfying schema constraints.

    """
    domain = "test"
    if isinstance(sub_cfg, Mapping):
        domain_cfg = sub_cfg.get("domain")
        if isinstance(domain_cfg, str) and domain_cfg:
            domain = domain_cfg
        elif (
            isinstance(domain_cfg, Sequence)
            and not isinstance(domain_cfg, (str, bytes, bytearray))
            and domain_cfg
        ):
            first = domain_cfg[0]
            if isinstance(first, str) and first:
                domain = first

    dummy_entity = f"{domain}.dummy"
    return _multi_or_single(sub_cfg, dummy_entity)


def _dummy_target_value(sub_cfg: object) -> dict[str, str]:
    """Derive dummy target mapping honoring entity domain filter.

    Args:
        sub_cfg: Target selector configuration mapping.

    Returns:
        Dictionary satisfying target selector schema.

    """
    domain = "test"
    if isinstance(sub_cfg, Mapping):
        entity_cfg = sub_cfg.get("entity")
        if isinstance(entity_cfg, Mapping):
            domain_cfg = entity_cfg.get("domain")
            if isinstance(domain_cfg, str) and domain_cfg:
                domain = domain_cfg
            elif (
                isinstance(domain_cfg, Sequence)
                and not isinstance(domain_cfg, (str, bytes, bytearray))
                and domain_cfg
            ):
                first = domain_cfg[0]
                if isinstance(first, str) and first:
                    domain = first

    return {ATTR_ENTITY_ID: f"{domain}.dummy"}


def get_ha_live_selectors() -> Mapping[str, type]:
    """Retrieve the live selector registry from Home Assistant Core.

    Returns:
        Mapping of selector type names to selector classes, or empty dict if unavailable.

    """
    try:
        from homeassistant.helpers import selector as ha_selector

        selectors = getattr(ha_selector, "SELECTORS", None)
        if isinstance(selectors, Mapping):
            return selectors
    except Exception:
        _LOGGER.debug("Home Assistant selector registry unavailable", exc_info=True)
    return {}


_CANDIDATE_DYNAMIC_DUMMIES: Final[tuple[object, ...]] = (
    _DEFAULT_DUMMY_VALUE,
    "",
    "dummy",
    0,
    False,
    {},
    [],
)


def generate_dummy_input_value(sel_cfg: object) -> object:
    """Generate a minimal valid mock value for a given Home Assistant selector config.

    Args:
        sel_cfg: Selector mapping from blueprint input definition.

    Returns:
        Dummy value appropriate for passing Home Assistant schema validation.

    """
    if not isinstance(sel_cfg, Mapping):
        return _DEFAULT_DUMMY_VALUE

    for sel_type, sub_cfg in sel_cfg.items():
        if sel_type in _ID_DUMMY_DEFAULTS:
            return _multi_or_single(sub_cfg, _ID_DUMMY_DEFAULTS[sel_type])
        if sel_type == SelectorType.ENTITY:
            return _dummy_entity_value(sub_cfg)
        if sel_type == SelectorType.TARGET:
            return _dummy_target_value(sub_cfg)
        if sel_type in _SIMPLE_DUMMY_SELECTORS:
            return copy.deepcopy(_SIMPLE_DUMMY_SELECTORS[sel_type])
        if (
            sel_type == SelectorType.CONSTANT
            and isinstance(sub_cfg, Mapping)
            and "value" in sub_cfg
        ):
            return copy.deepcopy(sub_cfg["value"])
        if sel_type == SelectorType.NUMBER:
            return _dummy_number_value(sub_cfg)
        if sel_type == SelectorType.SELECT:
            return _dummy_select_value(sub_cfg)
        if sel_type == SelectorType.COLOR_TEMP:
            return _dummy_color_temp_value(sub_cfg)
        if sel_type == SelectorType.COUNTRY:
            return _dummy_country_value(sub_cfg)
        if sel_type == SelectorType.LANGUAGE:
            return _dummy_language_value(sub_cfg)
        if sel_type == SelectorType.MEDIA:
            return _dummy_media_value(sub_cfg)
        if sel_type == SelectorType.CHOOSE:
            return _dummy_choose_value(sub_cfg)
        if sel_type == SelectorType.DEVICE_CLASS:
            return _dummy_device_class_value(sub_cfg)
        if sel_type == SelectorType.OBJECT:
            return _dummy_object_value(sub_cfg)

    # For any selector not explicitly handled (e.g. newly introduced in HA core),
    # dynamically probe against HA live selector schemas to avoid drift.
    for candidate in _CANDIDATE_DYNAMIC_DUMMIES:
        if _is_dummy_value_valid_for_selector(sel_cfg, candidate):
            return copy.deepcopy(candidate)

    return _DEFAULT_DUMMY_VALUE


_generate_dummy_input_value = generate_dummy_input_value


_EMPTY_UNSAFE_SELECTORS: Final[frozenset[str]] = frozenset(
    {
        SelectorType.TARGET,
        SelectorType.ENTITY,
        SelectorType.DEVICE,
        SelectorType.AREA,
        SelectorType.FLOOR,
        SelectorType.LABEL,
    }
)


def is_invalid_for_input_default(value: object, cfg: Mapping[str, object]) -> bool:
    """Check if a default value is null or empty for selectors requiring non-empty values.

    Args:
        value: Default value defined in the input metadata.
        cfg: Input configuration dictionary.

    Returns:
        True if the default value is unsafe/empty when substituted.

    """
    if value is None:
        return True
    if value in ("", {}, []):
        sel = cfg.get(_CONF_SELECTOR)
        if isinstance(sel, Mapping) and sel:
            if SelectorType.TARGET in sel:
                return not (isinstance(value, Mapping) and any(value.values()))
            if any(k in sel for k in _EMPTY_UNSAFE_SELECTORS):
                return True
            # In Home Assistant, action sequence [] is valid; "" or {} is not.
            # Non-target selectors (text, object, select, etc.) allow empty defaults.
            return value != [] if SelectorType.ACTION in sel else False

        # Untyped input (missing or empty selector) defaulting to empty is unsafe
        return True

    if isinstance(value, Mapping):
        sel = cfg.get(_CONF_SELECTOR)
        if isinstance(sel, Mapping) and SelectorType.TARGET in sel:
            return not any(value.values())

    return False


_is_invalid_for_input_default = is_invalid_for_input_default


def _iter_input_nodes(value: object, path: str) -> list[tuple[Input, str]]:
    """Recursively collect all !input nodes and their structural YAML paths.

    Args:
        value: Arbitrary structure parsed from YAML.
        path: Dot-delimited path of the current element.

    Returns:
        List of tuples of (Input, path).

    """
    results: list[tuple[Input, str]] = []
    if isinstance(value, Input):
        results.append((value, path))
    elif isinstance(value, Mapping):
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else str(k)
            results.extend(_iter_input_nodes(v, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for i, elem in enumerate(value):
            child_path = f"{path}[{i}]"
            results.extend(_iter_input_nodes(elem, child_path))
    return results


_HA_TARGET_FIELD_KEYS: Final[frozenset[str]] = _get_ha_target_field_keys()
_HA_SERVICE_ACTION_KEYS: Final[frozenset[str]] = frozenset({CONF_ACTION, CONF_SERVICE})
_HA_VARIABLE_BLOCK_KEYS: Final[frozenset[str]] = frozenset({CONF_VARIABLES, CONF_TRIGGER_VARIABLES})
_TRIGGER_PATH_SEGMENTS: Final[frozenset[str]] = frozenset(
    {CONF_TRIGGER, CONF_TRIGGERS, CONF_WAIT_FOR_TRIGGER}
)
_CONDITION_PATH_SEGMENTS: Final[frozenset[str]] = frozenset(
    {CONF_CONDITION, CONF_CONDITIONS, CONF_IF, CONF_WHILE, CONF_UNTIL}
)


def _get_path_segments(path: str) -> set[str]:
    """Extract individual key segments from a dot/bracket notation path.

    Args:
        path: Path string with dot or bracket notation.

    Returns:
        Set of segment names.

    """
    return {seg.split("[")[0] for seg in path.split(".") if seg}


def _check_target_inputs(
    value: object, path: str, empty_default_inputs: Mapping[str, object]
) -> list[str]:
    """Validate that target and entity fields do not reference empty-default inputs.

    Args:
        value: Node value to check.
        path: Path string of the node.
        empty_default_inputs: Mapping of input name to default value for invalid-default inputs.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    for inp, item_path in _iter_input_nodes(value, path):
        if inp.name in empty_default_inputs:
            default_repr = repr(empty_default_inputs[inp.name])
            segments = _get_path_segments(item_path)
            if _TRIGGER_PATH_SEGMENTS & segments:
                errors.append(
                    f"Unsafe '!input {inp.name}' at '{item_path}': trigger entity/device cannot "
                    f"default to an invalid value ({default_repr}). Provide a non-empty default "
                    "or make the input mandatory."
                )
            elif _CONDITION_PATH_SEGMENTS & segments:
                errors.append(
                    f"Unsafe '!input {inp.name}' at '{item_path}': condition entity/device cannot "
                    f"default to an invalid value ({default_repr}). Provide a non-empty default "
                    "or make the input mandatory."
                )
            else:
                errors.append(
                    f"Unsafe '!input {inp.name}' at '{item_path}': input defaults to an invalid "
                    f"target value ({default_repr}). Home Assistant requires a valid "
                    "entity/device ID or template. Use a Jinja template "
                    f"'{{{{ {inp.name} }}}}' referencing an automation variable instead, "
                    "or provide a non-empty default."
                )
    return errors


def _check_service_action_inputs(
    value: object, path: str, empty_default_inputs: Mapping[str, object]
) -> list[str]:
    """Validate that action or service names do not reference empty-default inputs.

    Args:
        value: Node value to check.
        path: Path string of the node.
        empty_default_inputs: Mapping of input name to default value for invalid-default inputs.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    for inp, item_path in _iter_input_nodes(value, path):
        if inp.name in empty_default_inputs:
            default_repr = repr(empty_default_inputs[inp.name])
            errors.append(
                f"Unsafe '!input {inp.name}' at '{item_path}': service/action name cannot "
                f"default to an empty value ({default_repr})."
            )
    return errors


def validate_safe_input_usages(
    obj: object,
    empty_default_inputs: Mapping[str, object] | None = None,
    path: str = _ROOT_PATH,
) -> list[str]:
    """Check for unsafe !input usages where the input defaults to empty/null in targets/actions.

    Args:
        obj: Blueprint data tree to traverse.
        empty_default_inputs: Optional mapping of input name to invalid default value.
        path: Current traversal path.

    Returns:
        List of error strings for unsafe usages.

    """
    if empty_default_inputs is None:
        if not isinstance(obj, Mapping):
            return []
        blueprint_meta = obj.get(CONF_BLUEPRINT)
        raw_inputs = blueprint_meta.get(CONF_INPUT) if isinstance(blueprint_meta, Mapping) else None
        input_configs = extract_input_configs(raw_inputs)
        empty_default_inputs = {
            k: v[CONF_DEFAULT]
            for k, v in input_configs.items()
            if CONF_DEFAULT in v and is_invalid_for_input_default(v[CONF_DEFAULT], v)
        }

    if not empty_default_inputs:
        return []

    errors: list[str] = []

    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in _HA_VARIABLE_BLOCK_KEYS:
                continue

            child_path = f"{path}.{k}" if path != _ROOT_PATH else str(k)

            if k in _HA_TARGET_FIELD_KEYS or k == CONF_TARGET:
                errors.extend(_check_target_inputs(v, child_path, empty_default_inputs))

            if k in _HA_SERVICE_ACTION_KEYS and not isinstance(v, (list, tuple, Mapping)):
                errors.extend(_check_service_action_inputs(v, child_path, empty_default_inputs))

            errors.extend(validate_safe_input_usages(v, empty_default_inputs, child_path))
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        for idx, item in enumerate(obj):
            errors.extend(validate_safe_input_usages(item, empty_default_inputs, f"{path}[{idx}]"))

    return errors


_TARGET_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)(?:target|data\.target)$")
_ENTITY_ID_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)entity_id(?:\[\d+\])?$")
_DEVICE_ID_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)device_id(?:\[\d+\])?$")
_AREA_ID_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)area_id(?:\[\d+\])?$")
_FLOOR_ID_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)floor_id(?:\[\d+\])?$")
_LABEL_ID_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)label_id(?:\[\d+\])?$")
_ACTION_ITEM_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\.)(?:action|actions|sequence|then|else|default)\[\d+\]$"
)
_ACTION_BLOCK_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\.)(?:action|actions|sequence|then|else|default)$"
)
_TRIGGER_ITEM_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)(?:trigger|triggers)\[\d+\]$")
_TRIGGER_BLOCK_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)(?:trigger|triggers)$")
_CONDITION_ITEM_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\.)(?:condition|conditions)\[\d+\]$"
)
_CONDITION_BLOCK_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)(?:condition|conditions)$")
_VARIABLES_BLOCK_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\.)(?:variables|trigger_variables)$"
)
_VARIABLE_CHILD_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\.)(?:variables|trigger_variables)\."
)


def _derive_value_for_path(path: str) -> object | None:
    """Derive compatible dummy value for a single observed blueprint usage path.

    Args:
        path: Dot-notation or bracket-notation structural path.

    Returns:
        Derived dummy value, or None if the path shape cannot be determined.

    """
    if _TARGET_PATH_RE.search(path):
        return {ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE}
    if _ENTITY_ID_PATH_RE.search(path):
        return _DEFAULT_DUMMY_VALUE
    if _DEVICE_ID_PATH_RE.search(path):
        return f"dummy_{ATTR_DEVICE_ID}"
    if _AREA_ID_PATH_RE.search(path):
        return f"dummy_{ATTR_AREA_ID}"
    if _FLOOR_ID_PATH_RE.search(path):
        return f"dummy_{ATTR_FLOOR_ID}"
    if _LABEL_ID_PATH_RE.search(path):
        return f"dummy_{ATTR_LABEL_ID}"
    if _ACTION_ITEM_PATH_RE.search(path):
        return {
            CONF_ACTION: "homeassistant.update_entity",
            CONF_TARGET: {ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE},
        }
    if _ACTION_BLOCK_PATH_RE.search(path):
        return []
    if _TRIGGER_ITEM_PATH_RE.search(path):
        return {CONF_TRIGGER: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE}
    if _TRIGGER_BLOCK_PATH_RE.search(path):
        return [{CONF_TRIGGER: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE}]
    if _CONDITION_ITEM_PATH_RE.search(path):
        return {CONF_CONDITION: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE, "state": "on"}
    if _CONDITION_BLOCK_PATH_RE.search(path):
        return [{CONF_CONDITION: "state", ATTR_ENTITY_ID: _DEFAULT_DUMMY_VALUE, "state": "on"}]
    if _VARIABLES_BLOCK_PATH_RE.search(path):
        return {}
    return _DEFAULT_DUMMY_VALUE if _VARIABLE_CHILD_PATH_RE.search(path) else None


def _is_dummy_value_valid_for_selector(sel_cfg: Mapping[str, object], dummy: object) -> bool:
    """Verify if the generated dummy value satisfies the Home Assistant selector schema.

    Args:
        sel_cfg: Selector configuration mapping.
        dummy: Generated candidate dummy value.

    Returns:
        True if the dummy value satisfies selector schema or if selector cannot be resolved,
        False if the selector schema explicitly rejects the dummy value.

    """
    if not isinstance(sel_cfg, Mapping) or not sel_cfg:
        return False

    sel_type = next(iter(sel_cfg.keys()), None)
    if not isinstance(sel_type, str):
        return False

    try:
        from homeassistant.helpers import selector as ha_selector
    except ImportError:
        return sel_type in _KNOWN_SELECTOR_TYPES

    registry = getattr(ha_selector, "SELECTORS", None)
    if registry is not None and sel_type not in registry:
        return sel_type in _KNOWN_SELECTOR_TYPES

    try:
        raw_selector: object = ha_selector.selector(dict(sel_cfg))
        if callable(raw_selector):
            raw_selector(dummy)
        return True
    except Exception as err:
        if "outside the event loop" in str(err):
            return True
        _LOGGER.debug(
            "Selector %r rejected dummy value %r: %s",
            sel_cfg,
            dummy,
            err,
        )
        return False


def derive_dummy_input_value(
    input_name: str,
    input_cfg: Mapping[str, object],
    blueprint_dict: Mapping[str, object],
) -> object | None:
    """Derive a valid dummy value for an input from its selector or observed usage.

    Args:
        input_name: Input key name.
        input_cfg: Input definition configuration dictionary.
        blueprint_dict: Parsed blueprint dictionary.

    Returns:
        A mock value compatible with schema validation, or None if the shape cannot be determined.

    """
    sel = input_cfg.get(_CONF_SELECTOR)
    if isinstance(sel, Mapping) and sel:
        dummy = generate_dummy_input_value(sel)
        if not _is_dummy_value_valid_for_selector(sel, dummy):
            _LOGGER.debug(
                "Generated dummy value %r failed schema validation for selector %r; "
                "skipping baseline validation",
                dummy,
                sel,
            )
            return None
        return dummy

    usages = [path for inp, path in _iter_input_nodes(blueprint_dict, "") if inp.name == input_name]

    if not usages:
        return _DEFAULT_DUMMY_VALUE

    derived_values: list[object] = []
    for path in usages:
        val = _derive_value_for_path(path)
        if val is None:
            return None
        derived_values.append(val)

    first = derived_values[0]
    return None if any(v != first for v in derived_values[1:]) else first


def _extract_target_names(target: nodes.Node, names: set[str]) -> None:
    """Extract variable names targeted by an assignment or iteration node.

    Args:
        target: Target Jinja AST node.
        names: Set to collect target name strings into.

    """
    if isinstance(target, nodes.Name):
        names.add(target.name)
    elif isinstance(target, (nodes.Tuple, nodes.List)):
        for item in target.items:
            _extract_target_names(item, names)


def _check_math_getattr_usage(
    node: nodes.Getattr,
    env: TemplateEnvironment,
    math_globals_desc: str,
) -> str:
    """Format an error message for an invalid math.<attr> expression.

    Args:
        node: Getattr AST node.
        env: Template environment.
        math_globals_desc: Description string of available math globals.

    Returns:
        Formatted error string.

    """
    attr_name = node.attr
    if attr_name in env.globals:
        return (
            f"line {node.lineno}: {_MATH_UNAVAILABLE_MSG} "
            f"'{attr_name}' is directly available as a global: use '{attr_name}' instead of "
            f"'{_MATH_MODULE}.{attr_name}'."
        )
    if attr_name in env.filters:
        return (
            f"line {node.lineno}: {_MATH_UNAVAILABLE_MSG} "
            f"'{attr_name}' is available as a filter: use '| {attr_name}' instead of "
            f"'{_MATH_MODULE}.{attr_name}'."
        )
    return (
        f"line {node.lineno}: {_MATH_UNAVAILABLE_MSG} "
        f"Use direct math globals/filters instead{math_globals_desc}."
    )


class _ScopedMathInspector:
    """Jinja AST inspector for undeclared math usages honoring local scopes and shadowing."""

    def __init__(self, env: TemplateEnvironment) -> None:
        """Initialize inspector with template environment.

        Args:
            env: Template environment.

        """
        self._env = env
        ha_math_globals = sorted(_PYTHON_MATH_NAMES & set(env.globals.keys()))
        self._math_globals_desc = (
            f" (available globals: {', '.join(ha_math_globals)})" if ha_math_globals else ""
        )
        self.errors: list[str] = []
        self.reported_math_lines: set[int] = set()

    def inspect(self, ast: nodes.Node) -> list[str]:
        """Inspect the AST and return collected error messages.

        Args:
            ast: Root Jinja AST node.

        Returns:
            List of compatibility error strings.

        """
        self._visit(ast, set())
        return self.errors

    def _check_math_getattr(self, node: nodes.Getattr, scope: set[str]) -> bool:
        """Check for math attribute access when math is not in scope.

        Args:
            node: Getattr node to check.
            scope: Currently active variable scope.

        Returns:
            True if an undeclared math attribute usage was reported.

        """
        if (
            isinstance(node.node, nodes.Name)
            and node.node.ctx == _AST_CTX_LOAD
            and node.node.name == _MATH_MODULE
            and _MATH_MODULE not in scope
            and _MATH_MODULE not in self._env.globals
        ):
            self.reported_math_lines.add(node.lineno)
            self.errors.append(_check_math_getattr_usage(node, self._env, self._math_globals_desc))
            return True
        return False

    def _check_math_call(self, node: nodes.Call, scope: set[str]) -> None:
        """Check for direct call of unsupported Python math functions.

        Args:
            node: Call node to check.
            scope: Currently active variable scope.

        """
        func_node = node.node
        if (
            isinstance(func_node, nodes.Name)
            and func_node.ctx == _AST_CTX_LOAD
            and func_node.name in _PYTHON_MATH_NAMES
            and func_node.name not in self._env.globals
            and func_node.name not in scope
        ):
            if func_node.name in self._env.filters:
                hint = (
                    f"'{func_node.name}' is registered as a filter: "
                    f"use '| {func_node.name}' instead"
                )
            else:
                hint = f"'{func_node.name}()' is not registered as a global function"
            self.errors.append(
                f"line {func_node.lineno}: '{func_node.name}()' is not available in "
                f"Home Assistant ({hint})."
            )

    def _check_math_name(self, node: nodes.Name, scope: set[str]) -> None:
        """Check for bare math module usage.

        Args:
            node: Name node to check.
            scope: Currently active variable scope.

        """
        if (
            node.ctx == _AST_CTX_LOAD
            and node.name == _MATH_MODULE
            and _MATH_MODULE not in scope
            and _MATH_MODULE not in self._env.globals
            and node.lineno not in self.reported_math_lines
        ):
            self.errors.append(
                f"line {node.lineno}: {_MATH_UNAVAILABLE_MSG} "
                f"Use direct math globals/filters instead{self._math_globals_desc}."
            )
            self.reported_math_lines.add(node.lineno)

    def _visit_for(self, node: nodes.For, scope: set[str]) -> None:
        """Visit for-loop honoring target variable scope.

        Args:
            node: For node to inspect.
            scope: Currently active variable scope.

        """
        self._visit(node.iter, scope)
        child_scope = set(scope)
        _extract_target_names(node.target, child_scope)
        for child in node.body:
            self._visit(child, child_scope)
        for child in node.else_:
            self._visit(child, scope)

    def _visit_macro(self, node: nodes.Macro, scope: set[str]) -> None:
        """Visit macro definition honoring argument variable scope.

        Args:
            node: Macro node to inspect.
            scope: Currently active variable scope.

        """
        scope.add(node.name)
        child_scope = set(scope)
        for arg in node.args:
            if isinstance(arg, nodes.Name):
                child_scope.add(arg.name)
        for default in node.defaults:
            self._visit(default, scope)
        for child in node.body:
            self._visit(child, child_scope)

    def _visit_with(self, node: nodes.With, scope: set[str]) -> None:
        """Visit with statement honoring bound variable scope.

        Args:
            node: With node to inspect.
            scope: Currently active variable scope.

        """
        for val in getattr(node, "values", ()):
            self._visit(val, scope)
        child_scope = set(scope)
        for target in getattr(node, "targets", ()):
            _extract_target_names(target, child_scope)
        for child in node.body:
            self._visit(child, child_scope)

    def _visit_assign(self, node: nodes.Assign, scope: set[str]) -> None:
        """Visit assignment statement updating current scope.

        Args:
            node: Assign node to inspect.
            scope: Currently active variable scope.

        """
        self._visit(node.node, scope)
        _extract_target_names(node.target, scope)

    def _visit_assign_block(self, node: nodes.AssignBlock, scope: set[str]) -> None:
        """Visit assignment block updating current scope.

        Args:
            node: AssignBlock node to inspect.
            scope: Currently active variable scope.

        """
        for child in node.body:
            self._visit(child, scope)
        _extract_target_names(node.target, scope)

    def _visit_import(self, node: nodes.Import, scope: set[str]) -> None:
        """Register imported module alias in current scope.

        Args:
            node: Import node to inspect.
            scope: Currently active variable scope.

        """
        if isinstance(node.target, str):
            scope.add(node.target)

    def _visit_from_import(self, node: nodes.FromImport, scope: set[str]) -> None:
        """Register imported names and aliases in current scope.

        Args:
            node: FromImport node to inspect.
            scope: Currently active variable scope.

        """
        for item in getattr(node, "names", ()):
            if isinstance(item, tuple) and len(item) == 2:
                scope.add(str(item[1]))
            elif isinstance(item, str):
                scope.add(item)

    def _visit_children(self, node: nodes.Node, scope: set[str]) -> None:
        """Recursively visit all child fields of an unhandled node.

        Args:
            node: AST node to inspect.
            scope: Currently active variable scope.

        """
        for _, field_val in node.iter_fields():
            if isinstance(field_val, list):
                for item in field_val:
                    self._visit(item, scope)
            elif isinstance(field_val, nodes.Node):
                self._visit(field_val, scope)

    def _visit(self, node: object, scope: set[str]) -> None:
        """Recursively inspect node for math usages honoring local scopes and shadowing.

        Args:
            node: Jinja AST node or object.
            scope: Set of variable names currently in scope.

        """
        if not isinstance(node, nodes.Node):
            return

        if isinstance(node, nodes.Getattr) and self._check_math_getattr(node, scope):
            return

        if isinstance(node, nodes.Call):
            self._check_math_call(node, scope)

        if isinstance(node, nodes.Name):
            self._check_math_name(node, scope)

        if isinstance(node, nodes.For):
            self._visit_for(node, scope)
        elif isinstance(node, nodes.Macro):
            self._visit_macro(node, scope)
        elif isinstance(node, nodes.With):
            self._visit_with(node, scope)
        elif isinstance(node, nodes.Assign):
            self._visit_assign(node, scope)
        elif isinstance(node, nodes.AssignBlock):
            self._visit_assign_block(node, scope)
        elif isinstance(node, nodes.Import):
            self._visit_import(node, scope)
        elif isinstance(node, nodes.FromImport):
            self._visit_from_import(node, scope)
        else:
            self._visit_children(node, scope)


def _inspect_scoped_math(
    node: object,
    env: TemplateEnvironment,
    current_scope: set[str],
    reported_math_lines: set[int],
    errors: list[str],
    math_globals_desc: str,
) -> None:
    """Recursively inspect node for math usages honoring local scopes and shadowing.

    Args:
        node: Jinja AST node or object.
        env: Template environment.
        current_scope: Set of variable names currently in scope.
        reported_math_lines: Set of line numbers where a math usage was already flagged.
        errors: Output list of error messages.
        math_globals_desc: Description string of available HA math globals.

    """
    inspector = _ScopedMathInspector(env)
    inspector.reported_math_lines = reported_math_lines
    inspector.errors = errors
    inspector._math_globals_desc = math_globals_desc
    inspector._visit(node, current_scope)


def _check_math_module_usages(
    ast: nodes.Node,
    env: TemplateEnvironment,
) -> list[str]:
    """Flag undeclared 'math' module usage honoring local scopes, shadowing, and aliases.

    Args:
        ast: Root Jinja AST node.
        env: Template environment.

    Returns:
        List of error strings.

    """
    return [] if _MATH_MODULE in env.globals else _ScopedMathInspector(env).inspect(ast)


def _check_mutating_method_calls(ast: nodes.Node) -> list[str]:
    """Flag mutating collection methods blocked by Home Assistant's sandboxed environment.

    Args:
        ast: Root Jinja AST node.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    for node in ast.find_all(nodes.Call):
        func_node = node.node
        if isinstance(func_node, nodes.Getattr) and func_node.attr in _HA_MUTABLE_METHOD_NAMES:
            errors.append(
                f"line {func_node.lineno}: calling mutating method '.{func_node.attr}()' is not "
                "allowed in Home Assistant templates."
            )
    return errors


def _is_jinja_template_name(tmpl_name: str) -> bool:
    """Check if an imported name targets a Jinja template rather than a Python module.

    Args:
        tmpl_name: The imported template name.

    Returns:
        True if the name has a .jinja extension, False otherwise.

    """
    return (
        tmpl_name.endswith(_ALLOWED_TEMPLATE_EXTENSION) and tmpl_name != _ALLOWED_TEMPLATE_EXTENSION
    )


def _extract_custom_template_paths(env: TemplateEnvironment | None) -> set[str] | None:
    """Extract valid custom template paths recognized by Home Assistant.

    Home Assistant populates custom templates into an in-memory loader dictionary
    (result[path] = content) by scanning custom_templates/**/*.jinja. This helper
    extracts the exact set of relative template paths known to the loader or
    present on disk.

    Args:
        env: The template environment, potentially containing a loader or hass instance.

    Returns:
        A set of valid relative template paths, or None if no environment/hass context
        is available to inspect template availability.

    """
    if env is None:
        return None

    loader = getattr(env, "loader", None)
    hass = getattr(env, "hass", None)
    if loader is None and hass is None:
        return None

    # 1. Extract paths from the loader (e.g. HassLoader.sources or loader.mapping)
    if loader is not None:
        sources = getattr(loader, "sources", None)
        if isinstance(sources, Mapping) and len(sources) > 0:
            return set(sources)
        mapping = getattr(loader, "mapping", None)
        if isinstance(mapping, Mapping) and len(mapping) > 0:
            return set(mapping)

    # 2. If loader sources are empty or absent, extract paths using Home Assistant's logic
    if hass is not None and hasattr(hass, "config") and hasattr(hass.config, "path"):
        jinja_path = hass.config.path(_CUSTOM_TEMPLATES_FOLDER)
        if os.path.isdir(jinja_path):
            jinja_root = Path(jinja_path)
            return {
                item.relative_to(jinja_root).as_posix()
                for item in jinja_root.rglob(f"*{_ALLOWED_TEMPLATE_EXTENSION}")
                if item.is_file() and item.stat().st_size <= MAX_CUSTOM_TEMPLATE_SIZE
            }
        return set()

    return set() if loader is not None else None


def _is_custom_template_present(
    tmpl_name: str,
    env: TemplateEnvironment | None,
    available_paths: set[str] | None = None,
) -> bool:
    """Check if an imported custom template exists in the loader or on disk.

    Args:
        tmpl_name: The imported template filename or relative path.
        env: The template environment, potentially containing a loader or hass reference.
        available_paths: Optional pre-extracted set of available custom template paths.

    Returns:
        True if the template exists or context is unavailable; False if known not to exist.

    """
    paths = available_paths if available_paths is not None else _extract_custom_template_paths(env)
    if paths is not None:
        return tmpl_name in paths

    pure_path = PurePosixPath(tmpl_name)
    return not (
        pure_path.is_absolute()
        or tmpl_name.startswith(("/", f"{_CUSTOM_TEMPLATES_FOLDER}/"))
        or ".." in pure_path.parts
    )


def _check_template_imports(
    ast: nodes.Node,
    env: TemplateEnvironment | None = None,
) -> list[str]:
    """Flag invalid template imports and verify custom template existence.

    Args:
        ast: Root Jinja AST node.
        env: Optional template environment for loader and registry inspection.

    Returns:
        List of error strings.

    """
    errors: list[str] = []
    available_paths = _extract_custom_template_paths(env)

    for node in ast.find_all((nodes.Import, nodes.FromImport)):
        template_node = getattr(node, "template", None)
        if isinstance(template_node, nodes.Const) and isinstance(template_node.value, str):
            tmpl_name = template_node.value
        elif isinstance(template_node, nodes.Name):
            tmpl_name = template_node.name
        else:
            tmpl_name = str(
                getattr(template_node, "value", getattr(template_node, "name", template_node or ""))
            )

        if not _is_jinja_template_name(tmpl_name):
            errors.append(
                f"line {node.lineno}: cannot import Python modules via '{tmpl_name}'; "
                f"only custom templates in '{_CUSTOM_TEMPLATES_FOLDER}' are supported."
            )
            continue

        if not _is_custom_template_present(tmpl_name, env, available_paths):
            errors.append(
                f"line {node.lineno}: custom template '{tmpl_name}' does not exist in "
                f"'{_CUSTOM_TEMPLATES_FOLDER}'."
            )

    return errors


def check_ha_template_ast_compatibility(
    ast: nodes.Node,
    env: TemplateEnvironment,
) -> list[str]:
    """Inspect Jinja2 AST for structures and math expressions incompatible with Home Assistant.

    Args:
        ast: Root Jinja AST node.
        env: Template environment.

    Returns:
        List of compatibility error strings.

    """
    errors: list[str] = []
    errors.extend(_check_math_module_usages(ast, env))
    errors.extend(_check_mutating_method_calls(ast))
    errors.extend(_check_template_imports(ast, env))
    return errors


_check_ha_template_ast_compatibility = check_ha_template_ast_compatibility


def extract_defined_inputs(input_dict: object) -> set[str]:
    """Extract all input keys defined in blueprint.input (including nested sections).

    Args:
        input_dict: The raw input mapping from the blueprint block.

    Returns:
        A set of all defined input key names.

    """
    keys: set[str] = set()
    if not isinstance(input_dict, Mapping):
        return keys

    for k, v in input_dict.items():
        if isinstance(v, Mapping) and CONF_INPUT in v and isinstance(v[CONF_INPUT], Mapping):
            keys.update(extract_defined_inputs(v[CONF_INPUT]))
        elif isinstance(k, str):
            keys.add(k)
    return keys


def extract_inputs_with_default(input_dict: object) -> set[str]:
    """Recursively extract defined input keys that specify a default value.

    Args:
        input_dict: Parsed input mapping from blueprint metadata.

    Returns:
        A set of input key names that define a default value.

    """
    keys: set[str] = set()
    if not isinstance(input_dict, Mapping):
        return keys

    for k, v in input_dict.items():
        if isinstance(v, Mapping):
            if CONF_INPUT in v and isinstance(v[CONF_INPUT], Mapping):
                keys.update(extract_inputs_with_default(v[CONF_INPUT]))
            elif CONF_DEFAULT in v and isinstance(k, str):
                keys.add(k)
    return keys


def extract_mandatory_inputs(input_dict: object) -> set[str]:
    """Recursively extract defined input keys that do not define a default value.

    Args:
        input_dict: Parsed input mapping from blueprint metadata.

    Returns:
        A set of mandatory input key names.

    """
    keys: set[str] = set()
    if not isinstance(input_dict, Mapping):
        return keys

    for k, v in input_dict.items():
        if isinstance(v, Mapping):
            if CONF_INPUT in v and isinstance(v[CONF_INPUT], Mapping):
                keys.update(extract_mandatory_inputs(v[CONF_INPUT]))
            elif CONF_DEFAULT not in v and isinstance(k, str):
                keys.add(k)
        elif isinstance(k, str):
            keys.add(k)
    return keys


def extract_input_configs(input_dict: object) -> dict[str, dict[str, object]]:
    """Extract input definitions mapping input_name -> config dict.

    Args:
        input_dict: The input mapping from blueprint metadata.

    Returns:
        Dictionary mapping input names to their configuration dicts.

    """
    configs: dict[str, dict[str, object]] = {}
    if not isinstance(input_dict, Mapping):
        return configs

    for k, v in input_dict.items():
        if isinstance(v, Mapping):
            if CONF_INPUT in v and isinstance(v[CONF_INPUT], Mapping):
                configs |= extract_input_configs(v[CONF_INPUT])
            elif isinstance(k, str):
                configs[k] = dict(v)
        elif isinstance(k, str):
            configs[k] = {}
    return configs


def extract_used_inputs(obj: object) -> list[str]:
    """Recursively find all !input references in the parsed structure.

    Args:
        obj: Parsed blueprint YAML data or nested object.

    Returns:
        A list of input names referenced via !input tags.

    """
    used: list[str] = []
    if isinstance(obj, Input):
        used.append(obj.name)
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if isinstance(k, Input):
                used.append(k.name)
            elif not isinstance(k, (str, bytes, bytearray)):
                used.extend(extract_used_inputs(k))
            used.extend(extract_used_inputs(v))
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        for item in obj:
            used.extend(extract_used_inputs(item))
    return used


def validate_input_references(data: dict[str, object]) -> str | None:
    """Verify that all !input tags reference defined blueprint inputs.

    Args:
        data: Parsed YAML dictionary of the blueprint.

    Returns:
        An error message if undefined inputs are referenced, or None if valid.

    """
    blueprint_meta = data.get(CONF_BLUEPRINT)
    if not isinstance(blueprint_meta, Mapping):
        return None

    defined = extract_defined_inputs(blueprint_meta.get(CONF_INPUT))
    used = extract_used_inputs(data)

    if undefined := sorted({name for name in used if name not in defined}):
        if len(undefined) == 1:
            return f"Undefined input referenced: '!input {undefined[0]}'"
        formatted = ", ".join(f"'!input {name}'" for name in undefined)
        return f"Undefined inputs referenced: {formatted}"

    return None


def build_template_path(path: str, key: object) -> str:
    """Build a concrete YAML-style path for a mapping entry.

    Args:
        path: Parent path string.
        key: Child dictionary key.

    Returns:
        Dot-delimited or indexed YAML path.

    """
    if not path:
        return str(key)
    if isinstance(key, str) and key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{key!r}]"


def normalize_content(content: str) -> str:
    r"""Normalize blueprint content for consistent hashing.

    Performs transport-level normalization (strips BOM, normalizes CRLF -> LF).

    Args:
        content: Raw YAML content string.

    Returns:
        Normalized YAML content.

    """
    if "\r" not in content and not content.startswith("\ufeff"):
        return content

    if content.startswith("\ufeff"):
        content = content[1:]

    return content.replace("\r\n", "\n").replace("\r", "\n")


def canonicalize_source_url(source_url: str) -> str:
    """Canonicalize non-empty string source_url to a stable canonical form.

    Args:
        source_url: Raw URL string.

    Returns:
        Canonicalized URL string.

    """
    clean_url = source_url.strip()
    if not clean_url:
        return ""
    try:
        return registry.canonicalize_url(registry.normalize_url(clean_url))
    except (ValueError, TypeError):
        return clean_url


def get_blueprint_schema(domain: str) -> vol.Schema | vol.All:
    """Return the appropriate Home Assistant blueprint schema for a given domain.

    Args:
        domain: The blueprint domain (automation, script, or template).

    Returns:
        The corresponding voluptuous Schema.

    """
    if domain == FunctionalDomain.AUTOMATION:
        return AUTOMATION_BLUEPRINT_SCHEMA
    if domain == FunctionalDomain.TEMPLATE:
        if isinstance(TEMPLATE_BLUEPRINT_SCHEMA, vol.Schema):
            return TEMPLATE_BLUEPRINT_SCHEMA
        return BLUEPRINT_SCHEMA
    return BLUEPRINT_SCHEMA


def coerce_empty_selectors(data: object) -> None:
    """Coerce empty selector configurations with None values to empty dicts.

    Args:
        data: Arbitrary structured blueprint data to traverse and mutate.

    """
    if isinstance(data, dict):
        selector_data = data.get(_CONF_SELECTOR)
        if isinstance(selector_data, dict):
            for sel_type, sel_val in selector_data.items():
                if sel_val is None:
                    selector_data[sel_type] = {}
        for value in data.values():
            coerce_empty_selectors(value)
    elif isinstance(data, list):
        for item in data:
            coerce_empty_selectors(item)


def ensure_source_url_cached(
    content: str,
    source_url: str,
    *,
    normalize_fn: Callable[[str], str] | None = None,
    dump_fn: Callable[[dict | list], str] | None = None,
    filter_paths: frozenset[tuple[str, ...]] | None = None,
) -> str:
    """Implementation of source URL normalization.

    Args:
        content: Raw YAML blueprint content string.
        source_url: Canonical source URL string.
        normalize_fn: Optional custom normalization callable.
        dump_fn: Optional custom YAML dump callable.
        filter_paths: Optional selector filter paths to coerce singleton mappings.

    Returns:
        Normalized content string with source_url injected into blueprint metadata.

    """
    actual_normalize = normalize_fn or normalize_content
    actual_dump = dump_fn or yaml_util.dump
    source_url = source_url.strip()

    try:
        parsed = yaml_util.parse_yaml(content)
    except HomeAssistantError:
        parsed = None

    if not isinstance(parsed, dict) or CONF_BLUEPRINT not in parsed:
        return actual_normalize(content)

    blueprint_info = parsed[CONF_BLUEPRINT]
    if not isinstance(blueprint_info, dict):
        return actual_normalize(content)

    had_source_url = _CONF_SOURCE_URL in blueprint_info
    if had_source_url:
        blueprint_info[_CONF_SOURCE_URL] = source_url

    target_data: object = parsed
    try:
        domain = blueprint_info.get(CONF_DOMAIN, FunctionalDomain.AUTOMATION)
        schema = get_blueprint_schema(str(domain))
        coerce_empty_selectors(parsed)
        normalized = schema(parsed)
        actual_filter_paths = (
            filter_paths if filter_paths is not None else get_selector_filter_paths()
        )
        target_data = stabilize_yaml_structure(parsed, normalized, filter_paths=actual_filter_paths)
    except (vol.Invalid, KeyError, TypeError, ValueError) as err:
        _LOGGER.debug(
            "Semantic normalization skipped for %s (falling back to canonical YAML): %s",
            redact_url(source_url),
            err,
        )

    if isinstance(target_data, dict):
        bp_dict = target_data.get(CONF_BLUEPRINT)
        if isinstance(bp_dict, dict):
            bp_dict[_CONF_SOURCE_URL] = source_url

    if isinstance(target_data, (dict, list)):
        try:
            return actual_dump(target_data)
        except Exception as err:
            _LOGGER.warning(
                "YAML canonicalization failed for %s: %s",
                redact_url(source_url),
                err,
            )
            return actual_normalize(content)
    return actual_normalize(content)


def ensure_source_url(
    content: object,
    source_url: object,
    *,
    normalize_fn: Callable[[str], str] | None = None,
    dump_fn: Callable[[dict | list], str] | None = None,
    filter_paths: frozenset[tuple[str, ...]] | None = None,
) -> str:
    """Ensure the target source_url is present in the blueprint metadata.

    Args:
        content: Raw YAML blueprint content.
        source_url: Target URL to enforce in the content.
        normalize_fn: Optional custom normalization callable.
        dump_fn: Optional custom YAML dump callable.
        filter_paths: Optional selector filter paths to coerce singleton mappings.

    Returns:
        YAML content with source_url present in canonical normalized form.

    """
    actual_normalize = normalize_fn or normalize_content
    if not isinstance(content, str):
        _LOGGER.debug("Non-string content passed to _ensure_source_url: %s", type(content))
        return ""
    if not isinstance(source_url, str) or not source_url.strip():
        _LOGGER.debug(
            "Non-string or empty source_url passed to _ensure_source_url: %s", type(source_url)
        )
        return actual_normalize(content)

    return ensure_source_url_cached(
        content,
        source_url.strip(),
        normalize_fn=actual_normalize,
        dump_fn=dump_fn,
        filter_paths=filter_paths,
    )


def hash_content(
    content: str,
    source_url: object = None,
    already_normalized: bool = False,
    *,
    filter_paths: frozenset[tuple[str, ...]] | None = None,
) -> str:
    """Calculate a deterministic SHA-256 hash of normalized content.

    Args:
        content: The raw YAML string to hash.
        source_url: Optional source URL to trigger identity-aware hashing.
        already_normalized: If True, bypass normalization steps and hash raw content.
        filter_paths: Optional pre-resolved selector filter paths.  When supplied
            the value is forwarded to ``ensure_source_url_cached`` so the deep
            registry walk inside ``get_selector_filter_paths`` is skipped for
            this call.

    Returns:
        The SHA-256 hex digest of the normalized content.

    """
    if already_normalized:
        return hashlib.sha256(content.encode(_UTF8_ENCODING)).hexdigest()

    if not isinstance(source_url, str) or not source_url.strip():
        return hashlib.sha256(normalize_content(content).encode(_UTF8_ENCODING)).hexdigest()

    canonical_source_url = canonicalize_source_url(source_url)
    normalized = ensure_source_url_cached(content, canonical_source_url, filter_paths=filter_paths)
    return hashlib.sha256(normalized.encode(_UTF8_ENCODING)).hexdigest()


def _stabilize_dict_structure(
    orig_dict: dict[object, object],
    norm_dict: dict[object, object],
    selector_path: tuple[str, ...] | None,
    filter_paths: frozenset[tuple[str, ...]],
) -> dict[object, object]:
    """Recursively stabilize dictionary structures preserving original order or selector sorting.

    Args:
        orig_dict: Original dictionary mapping.
        norm_dict: Normalized dictionary mapping.
        selector_path: Accumulated path within a selector block, or None.
        filter_paths: Set of selector filter paths.

    Returns:
        Stabilized dictionary with preserved or sorted key ordering.

    """
    if selector_path is not None:
        sorted_keys = sorted(norm_dict.keys(), key=str)
        return {
            k: stabilize_yaml_structure(
                orig_dict.get(k),
                norm_dict[k],
                selector_path=(*selector_path, str(k)),
                allow_singleton_list_coercion=(*selector_path, str(k)) in filter_paths,
                filter_paths=filter_paths,
            )
            for k in sorted_keys
        }

    res: dict[object, object] = {
        k: stabilize_yaml_structure(
            orig_val,
            norm_dict[k],
            selector_path=() if k == _CONF_SELECTOR else None,
            allow_singleton_list_coercion=False,
            filter_paths=filter_paths,
        )
        for k, orig_val in orig_dict.items()
        if k in norm_dict
    }
    for key in sorted([k for k in norm_dict if k not in res], key=str):
        res[key] = stabilize_yaml_structure(
            norm_dict[key],
            norm_dict[key],
            selector_path=() if key == _CONF_SELECTOR else None,
            allow_singleton_list_coercion=False,
            filter_paths=filter_paths,
        )
    return res


def _stabilize_list_structure(
    orig_data: object,
    normalized_data: list[object],
    selector_path: tuple[str, ...] | None,
    allow_singleton_list_coercion: bool,
    filter_paths: frozenset[tuple[str, ...]],
) -> list[object]:
    """Recursively stabilize list structures handling singleton list coercion.

    Args:
        orig_data: Original unnormalized object.
        normalized_data: List processed by schema.
        selector_path: Accumulated path within a selector block, or None.
        allow_singleton_list_coercion: Whether singleton list coercion applies.
        filter_paths: Set of selector filter paths.

    Returns:
        Stabilized list of items.

    """
    if isinstance(orig_data, list):
        orig_list = orig_data
    elif (
        allow_singleton_list_coercion
        and isinstance(orig_data, dict)
        and len(normalized_data) == 1
        and isinstance(normalized_data[0], dict)
    ):
        orig_list = [orig_data]
    else:
        orig_list = []

    return [
        stabilize_yaml_structure(
            orig_list[i] if i < len(orig_list) else None,
            item,
            selector_path=selector_path,
            allow_singleton_list_coercion=False,
            filter_paths=filter_paths,
        )
        for i, item in enumerate(normalized_data)
    ]


def stabilize_yaml_structure(
    orig_data: object,
    normalized_data: object,
    selector_path: tuple[str, ...] | None = None,
    allow_singleton_list_coercion: bool = False,
    filter_paths: frozenset[tuple[str, ...]] | None = None,
) -> object:
    """Recursively update normalized structures using original key ordering.

    Args:
        orig_data: Original unnormalized object.
        normalized_data: Object processed by voluptuous schema.
        selector_path: Accumulated path within a selector block.
        allow_singleton_list_coercion: Whether singleton list coercion applies.
        filter_paths: Set of selector filter paths.

    Returns:
        Stabilized Python object ready for yaml dumping.

    """
    effective_filter_paths = (
        filter_paths if filter_paths is not None else get_selector_filter_paths()
    )

    if isinstance(normalized_data, dict):
        norm_dict = dict(normalized_data.items())
        orig_dict = dict(orig_data.items()) if isinstance(orig_data, dict) else {}
        return _stabilize_dict_structure(
            orig_dict, norm_dict, selector_path, effective_filter_paths
        )

    if isinstance(normalized_data, list):
        return _stabilize_list_structure(
            orig_data,
            normalized_data,
            selector_path,
            allow_singleton_list_coercion,
            effective_filter_paths,
        )

    return normalized_data


def extract_blueprint_text(content: str) -> str:
    """Extract only the blueprint block text to avoid parsing huge YAMLs.

    Args:
        content: Full YAML file content string.

    Returns:
        Extracted blueprint block string or full content.

    """
    lines = content.splitlines(keepends=True)
    blueprint_lines: list[str] = []
    in_blueprint = False
    for line in lines:
        if line.startswith("blueprint:"):
            in_blueprint = True
            blueprint_lines.append(line)
        elif in_blueprint:
            if line.strip() and line[0] not in (" ", "\t", "#"):
                break
            blueprint_lines.append(line)
    return "".join(blueprint_lines) if in_blueprint else content


def get_blueprint_block(
    path: str,
    content: str | None = None,
    parsed_data: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Extract the blueprint block from YAML content or pre-parsed data.

    Args:
        path: Path to the blueprint file on disk.
        content: Raw YAML content string (optional).
        parsed_data: Pre-parsed dictionary (optional).

    Returns:
        The blueprint dictionary block, or None if invalid.

    """
    parsed = parsed_data
    if not parsed and content:
        content_to_parse = extract_blueprint_text(content)
        try:
            parsed = yaml_util.parse_yaml(content_to_parse)
        except HomeAssistantError:
            try:
                parsed = yaml_util.parse_yaml(content)
            except HomeAssistantError as err:
                _LOGGER.warning("Failed to parse blueprint at %s", path)
                _LOGGER.debug("Blueprint parse error at %s: %s", path, err)
                return None

    if not isinstance(parsed, dict):
        _LOGGER.debug(
            "Skipping blueprint at %s: parsed YAML is not a mapping (got %s)",
            path,
            type(parsed).__name__,
        )
        return None

    if CONF_BLUEPRINT not in parsed:
        _LOGGER.debug(
            "Skipping blueprint at %s: missing top-level 'blueprint' key",
            path,
        )
        return None

    bp_info = parsed[CONF_BLUEPRINT]
    if not isinstance(bp_info, dict):
        _LOGGER.debug(
            "Skipping blueprint at %s: 'blueprint' key is not a mapping (got %s)",
            path,
            type(bp_info).__name__,
        )
        return None

    return {str(k): v for k, v in bp_info.items()} if isinstance(bp_info, dict) else None


def read_and_diff(local_path: str, remote_text: str, source_url: str) -> str:
    """Read and diff local vs remote content with normalization.

    Args:
        local_path: Path to the local blueprint file.
        remote_text: Raw remote content fetched from Git.
        source_url: The source URL to ensure is present in the remote.

    Returns:
        A unified diff string.

    """
    with open(local_path, encoding=_UTF8_ENCODING) as f:
        local_text = f.read()

    local_text = ensure_source_url(local_text, source_url)
    remote_text = ensure_source_url(remote_text, source_url)

    local_lines = local_text.splitlines(keepends=True)
    remote_lines = remote_text.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            local_lines,
            remote_lines,
            fromfile="local",
            tofile="remote",
        )
    )


def get_affected_entities(
    configs: Mapping[str, Mapping[str, object]],
    key: str,
) -> list[str]:
    """Find entities using a specific input key.

    Args:
        configs: Mapping of entity_id -> input mapping.
        key: Input key name.

    Returns:
        List of entity IDs using the input key.

    """
    return [eid for eid, inputs in configs.items() if key in inputs]


def is_input_mandatory(props: object) -> bool:
    """Check if an input schema property dictionary represents a mandatory input.

    Args:
        props: Input schema properties dictionary.

    Returns:
        True if the input is mandatory.

    """
    if not isinstance(props, dict):
        return True
    if _CONF_MANDATORY in props:
        return bool(props.get(_CONF_MANDATORY))
    return CONF_DEFAULT not in props


def detect_new_mandatory_inputs(
    old_schema: Mapping[str, object],
    new_schema: Mapping[str, object],
) -> list[StructuredRisk]:
    """Detect new mandatory inputs introduced in the schema.

    Args:
        old_schema: Previous schema dictionary.
        new_schema: New schema dictionary.

    Returns:
        List of structured risks for new mandatory inputs.

    """
    risks: list[StructuredRisk] = []
    for key, props in new_schema.items():
        if is_input_mandatory(props):
            old_props = old_schema.get(key)
            old_mandatory = is_input_mandatory(old_props) if old_props is not None else False
            if not old_mandatory:
                risks.append({"type": BlueprintRiskType.NEW_MANDATORY, "args": {CONF_INPUT: key}})
    return risks


def detect_missing_inputs(
    new_schema: Mapping[str, Mapping[str, object]],
    configs: Mapping[str, Mapping[str, object]],
) -> list[StructuredRisk]:
    """Detect missing mandatory inputs for existing entities.

    Args:
        new_schema: New schema dictionary.
        configs: Existing entity configurations.

    Returns:
        List of structured risks for missing mandatory inputs.

    """
    risks: list[StructuredRisk] = []
    for entity_id, inputs in configs.items():
        risks.extend(
            {
                "type": BlueprintRiskType.MISSING_INPUT,
                "args": {"entity": entity_id, CONF_INPUT: key},
            }
            for key, props in new_schema.items()
            if is_input_mandatory(props) and key not in inputs
        )
    return risks


def dedupe_risks(risks: Iterable[StructuredRisk]) -> list[StructuredRisk]:
    """De-duplicate risks by type and arguments.

    Args:
        risks: An iterable of structured risks.

    Returns:
        A list of unique structured risks.

    """
    seen: set[tuple[BlueprintRiskType, bytes]] = set()
    unique_risks: list[StructuredRisk] = []
    for risk in risks:
        if not isinstance(risk, dict) or "type" not in risk or "args" not in risk:
            _LOGGER.debug("Skipping malformed risk: %s", risk)
            continue

        key = (
            risk["type"],
            orjson.dumps(risk["args"], option=orjson.OPT_SORT_KEYS),
        )
        if key not in seen:
            seen.add(key)
            unique_risks.append(risk)
    return unique_risks
