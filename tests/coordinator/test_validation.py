"""Tests for coordinator behavior during blueprint validation and hub interactions.

This module provides focused testing for the coordination logic between the
blueprints_updater integration and the Home Assistant blueprint hub, ensuring
robust fail-safe mechanisms are in place during compatibility checks.
"""

import asyncio
import os
import socket
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.blueprint_validation import (
    ensure_source_url,
    normalize_content,
)
from custom_components.blueprints_updater.const import (
    ERROR_SEPARATOR,
    BlueprintRiskType,
    FilterMode,
    FunctionalDomain,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.utils import (
    get_validated_filter_mode,
    get_validated_selected_blueprints,
)


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator used in validation tests."""
    entry = MagicMock()
    entry.entry_id = "test_entry_validation"
    entry.options = {}
    entry.data = {}

    coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    coord.hass = hass
    coord.setup_complete = True
    coord.data = {}
    coord._translations = {}
    coord._blueprint_validate_lock = asyncio.Lock()
    return coord


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_isolated_from_hub(hass, coordinator):
    """Candidate validation never publishes content to the shared blueprint hub."""
    relative_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"

    mock_hub = MagicMock()
    original_bp = MagicMock()
    mock_hub._blueprints = {"test.yaml": original_bp}

    hass.data["blueprint"] = {FunctionalDomain.AUTOMATION: mock_hub}

    configs: dict[str, dict[str, Any]] = {
        "automation.test": {
            "alias": "Existing",
            "use_blueprint": {"path": relative_path, "input": {}},
        }
    }
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:

        async def check_during_validation(*args, **kwargs):
            """Check shared hub state during validation."""
            assert mock_hub._blueprints["test.yaml"] is original_bp
            assert "use_blueprint" not in kwargs["config"]

        mock_validate.side_effect = check_during_validation

        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs
        )
        assert risks == []
        mock_validate.assert_awaited_once_with(
            hass,
            config_key="automation.test",
            config={"alias": "Existing"},
        )

        assert mock_hub._blueprints["test.yaml"] == original_bp

    mock_hub._blueprints = {}
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(side_effect=HomeAssistantError("Validation failed")),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs
        )
        assert len(risks) == 1
        assert "Validation failed" in risks[0]["args"]["error"]
        assert "test.yaml" not in mock_hub._blueprints


@pytest.mark.asyncio
async def test_process_blueprint_content_error_handling(coordinator):
    """Test error handling in content processing.

    Covers invalid blueprint handling, YAML syntax errors, and schema validation error handling.
    """
    info: dict[str, Any] = {
        "relative_path": "test.yaml",
        "name": "Test BP",
        "local_hash": "old_hash",
    }

    path1 = "automation/invalid.yaml"
    coordinator.data[path1] = dict(info)
    await coordinator._process_blueprint_content(
        path1, info, "only_non_blueprint_data: True", "etag", "url", [], set()
    )
    assert coordinator.data[path1]["last_error"] == "invalid_blueprint"

    path2 = "automation/syntax.yaml"
    coordinator.data[path2] = dict(info)
    await coordinator._process_blueprint_content(
        path2, info, "invalid: yaml: [data", "etag", "url", [], set()
    )
    assert coordinator.data[path2]["last_error"].startswith(f"yaml_syntax_error{ERROR_SEPARATOR}")

    path3 = "automation/schema.yaml"
    coordinator.data[path3] = dict(info)
    with patch(
        "custom_components.blueprints_updater.coordinator.Blueprint",
        side_effect=InvalidBlueprint(
            FunctionalDomain.AUTOMATION, "test", {}, "Mock Schema Failure"
        ),
    ):
        await coordinator._process_blueprint_content(
            path3,
            info,
            "blueprint:\n  name: Test\n  domain: automation\n",
            "etag",
            "url",
            [],
            set(),
        )
        assert coordinator.data[path3]["last_error"].startswith(
            f"blueprint_validation_error{ERROR_SEPARATOR}"
        )
        assert "Mock Schema Failure" in coordinator.data[path3]["last_error"]

    path4 = "automation/template.yaml"
    coordinator.data[path4] = dict(info)
    await coordinator._process_blueprint_content(
        path4,
        info,
        """
blueprint:
  name: Test
  domain: automation
  description: "Documentation with an unfinished example: {{ value"
  input: {}
variables:
  broken: "{{ value | }}"
""",
        "https://example.com/blueprint.yaml",
        [],
        set(),
    )
    assert coordinator.data[path4]["last_error"].startswith(
        f"blueprint_validation_error{ERROR_SEPARATOR}"
    )
    assert "variables.broken" in coordinator.data[path4]["last_error"]
    assert coordinator.data[path4]["remote_hash"] is None
    assert coordinator.data[path4]["updatable"] is False


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_unexpected_error(hass, coordinator):
    """Verify that unexpected errors during validation are caught and reported as SYSTEM_ERROR.

    Ensures that the catch-all Exception block handles internal logic failure gracefully.
    """
    relative_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"
    configs: dict[str, dict[str, Any]] = {
        "automation.test": {
            "alias": "Existing",
            "use_blueprint": {"path": relative_path, "input": {}},
        }
    }

    with patch(
        "custom_components.blueprints_updater.coordinator.yaml_util.parse_yaml",
        side_effect=RuntimeError("Unexpected internal failure"),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs
        )
        assert len(risks) == 1
        assert risks[0]["type"] == BlueprintRiskType.SYSTEM_ERROR
        assert "Unexpected internal failure" in risks[0]["args"]["error"]


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_malformed_path(coordinator):
    """Verify that a relative_path without a domain folder returns a SYSTEM_ERROR.

    Ensures that we don't silently skip validation or misparse filenames as domains.
    """
    relative_path = "invalid_path.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"
    configs: dict[str, dict[str, Any]] = {}

    risks = await coordinator._async_validate_blueprint_consumers(relative_path, content, configs)

    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.SYSTEM_ERROR
    assert "Malformed blueprint path" in risks[0]["args"]["error"]
    assert risks[0]["args"]["path"] == relative_path


def test_is_safe_path(hass, coordinator):
    """Test _is_safe_path logic."""
    coordinator._is_safe_path = BlueprintUpdateCoordinator._is_safe_path.__get__(coordinator)

    base_config = "/home/hass/config"
    blueprints_dir = os.path.join(base_config, "blueprints")

    hass.config.path.side_effect = lambda *args: os.path.join(base_config, *args)

    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.realpath",
        side_effect=os.path.normpath,
    ):
        assert coordinator._is_safe_path(os.path.join(blueprints_dir, "automation/test.yaml"))
        assert coordinator._is_safe_path(os.path.join(blueprints_dir, "script/another.yaml"))
        assert not coordinator._is_safe_path(os.path.join(base_config, "secrets.yaml"))
        assert not coordinator._is_safe_path("/etc/passwd")
        assert not coordinator._is_safe_path(os.path.join(blueprints_dir, "../secrets.yaml"))


@pytest.mark.asyncio
async def test_is_safe_url(coordinator):
    """Test _is_safe_url logic."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    coord: Any = coordinator

    addr_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
    with patch("socket.getaddrinfo", return_value=addr_info):
        assert await coord._is_safe_url("https://github.com/user/repo")
        assert await coord._is_safe_url("https://raw.githubusercontent.com/user/repo/main/bp.yaml")
        assert await coord._is_safe_url("https://gist.github.com/user/gistid")
        assert await coord._is_safe_url("https://community.home-assistant.io/t/topic/123")
        assert await coord._is_safe_url("https://gitlab.com/user/repo/-/raw/main/bp.yaml")
        assert await coord._is_safe_url("https://bitbucket.org/user/repo/raw/main/bp.yaml")

        assert not await coord._is_safe_url("http://github.com/somepath")

    with patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert not await coord._is_safe_url("http://localhost:8123")
        assert not await coord._is_safe_url("http://homeassistant.local:8123")
        assert not await coord._is_safe_url("http://test.example/api")
        assert not await coord._is_safe_url("http://192.168.1.1/admin")
        assert not await coord._is_safe_url("http://127.0.0.1/admin")


@pytest.mark.asyncio
async def test_is_safe_url_dns_resolution(coordinator):
    """Test _is_safe_url logic with DNS resolution."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    coord: Any = coordinator

    with patch("socket.getaddrinfo") as mock_getaddr:
        mock_getaddr.return_value = [(None, None, None, None, ("192.168.1.50", 0))]
        assert not await coord._is_safe_url("https://malicious-dns.com/bp.yaml")

    with patch("socket.getaddrinfo") as mock_getaddr:
        mock_getaddr.return_value = [(None, None, None, None, ("8.8.8.8", 0))]
        assert await coord._is_safe_url("https://google.com/bp.yaml")
    with patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert not await coord._is_safe_url("https://unresolvable.com/bp.yaml")


@pytest.mark.asyncio
async def test_is_safe_url_caches_canonical_idna_hostname(coordinator):
    """Equivalent Unicode and punycode URLs share one canonical safety result."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    coordinator._perform_safe_hostname_check = AsyncMock(return_value=True)

    assert await coordinator._is_safe_url("https://BÜCHER.com./blueprint.yaml")
    assert await coordinator._is_safe_url("https://xn--bcher-kva.com/blueprint.yaml")
    coordinator._perform_safe_hostname_check.assert_awaited_once_with("xn--bcher-kva.com")


@pytest.mark.asyncio
async def test_background_refresh_clears_inflight_hostname_result(coordinator):
    """A refresh clears DNS results that began before cache invalidation."""

    class _NotifyingLock:
        """An async lock that exposes when refresh owns it."""

        def __init__(self) -> None:
            """Initialize the lock and acquisition signal."""
            self._lock = asyncio.Lock()
            self.entered = asyncio.Event()

        async def __aenter__(self) -> "_NotifyingLock":
            """Acquire the lock and signal ownership."""
            await self._lock.acquire()
            self.entered.set()
            return self

        async def __aexit__(self, *_args: Any) -> None:
            """Release the lock."""
            self._lock.release()

    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()

    async def _delayed_hostname_check(_hostname: str) -> bool:
        """Hold a DNS result until refresh begins invalidating the cache."""
        lookup_started.set()
        await finish_lookup.wait()
        return True

    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    coordinator._perform_safe_hostname_check = AsyncMock(side_effect=_delayed_hostname_check)
    refresh_lock = _NotifyingLock()
    coordinator._refresh_lock = refresh_lock

    lookup_task = asyncio.create_task(
        coordinator._is_safe_url("https://example.com/blueprint.yaml")
    )
    await lookup_started.wait()
    refresh_task = asyncio.create_task(
        coordinator._async_background_refresh({}, coordinator._refresh_generation)
    )
    await refresh_lock.entered.wait()

    finish_lookup.set()
    assert await lookup_task
    await refresh_task

    assert coordinator._safe_hostname_cache == {}


def test_get_validated_filter_mode_normalization():
    """Test that filter mode is normalized (lowercase and stripped) to FilterMode enum."""
    assert get_validated_filter_mode("  All  ") is FilterMode.ALL
    assert get_validated_filter_mode("WHITELIST") is FilterMode.WHITELIST
    assert get_validated_filter_mode("Blacklist") is FilterMode.BLACKLIST
    assert get_validated_filter_mode(FilterMode.WHITELIST) is FilterMode.WHITELIST
    assert get_validated_filter_mode("invalid") is FilterMode.ALL
    assert get_validated_filter_mode(None) is FilterMode.ALL
    assert get_validated_filter_mode(123) is FilterMode.ALL


def test_get_validated_selected_blueprints_hardening():
    """Test the hardening of _get_validated_selected_blueprints."""
    assert get_validated_selected_blueprints(None) == []

    res = get_validated_selected_blueprints("  path/to/bp.yaml  ")
    assert res == ["path/to/bp.yaml"]
    assert get_validated_selected_blueprints("   ") == []
    assert get_validated_selected_blueprints(["a", " b ", None, ""]) == ["a", "b"]
    assert get_validated_selected_blueprints(("a", "b")) == ["a", "b"]

    with patch("custom_components.blueprints_updater.utils._LOGGER") as mock_logger:
        assert get_validated_selected_blueprints({"key": "value"}) == []
        mock_logger.error.assert_called()
        assert "mapping" in mock_logger.error.call_args[0][0]
    with patch("custom_components.blueprints_updater.utils._LOGGER") as mock_logger:
        assert get_validated_selected_blueprints(123) == []
        mock_logger.error.assert_called()
        assert "Invalid type" in mock_logger.error.call_args[0][0]


def test_ensure_source_url_indented_key(coordinator):
    """Test that indented blueprint keys do NOT trigger injection."""
    source_url = "https://url.com/blueprint.yaml"
    content = """
not_blueprint:
  something: else
  blueprint:
    nested: true
"""
    expected = normalize_content(content)
    assert ensure_source_url(content, source_url) == expected


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_voluptuous_error(hass, coordinator):
    """Verify that voluptuous validation failures are registered as COMPATIBILITY risks."""
    relative_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"

    mock_hub = MagicMock()
    mock_hub._blueprints = {}
    hass.data["blueprint"] = {FunctionalDomain.AUTOMATION: mock_hub}

    configs = {
        "automation.test": {
            "use_blueprint": {"path": relative_path, "input": {}},
        }
    }

    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(side_effect=vol.Invalid("Invalid selector parameter")),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs
        )
        assert len(risks) == 1
        assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
        assert "Invalid selector parameter" in risks[0]["args"]["error"]


def test_validate_blueprint_rejects_unsafe_empty_default_target(coordinator: Any) -> None:
    """Validate that !input referencing an empty default in target entity_id is rejected."""
    raw_yaml = """
blueprint:
  name: Test Unsafe Target
  domain: automation
  input:
    target_lamp:
      name: Lamp
      default: ""
      selector:
        entity:
          domain: light
trigger: []
action:
  - action: light.turn_on
    target:
      entity_id: !input target_lamp
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "automation")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "Unsafe '!input target_lamp'" in res


def test_validate_blueprint_rejects_unsafe_empty_default_service(coordinator: Any) -> None:
    """Validate that !input referencing an empty default in service name is rejected."""
    raw_yaml = """
blueprint:
  name: Test Unsafe Service
  domain: automation
  input:
    custom_service:
      name: Service
      default: ""
trigger: []
action:
  - action: !input custom_service
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "automation")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "Unsafe '!input custom_service'" in res


def test_validate_blueprint_rejects_template_with_math_module(coordinator: Any) -> None:
    """Validate that Jinja2 templates using 'math.' module are caught."""
    raw_yaml = """
blueprint:
  name: Math Module Script
  domain: script
  input: {}
sequence:
  - variables:
      val: "{{ math.sqrt(16) }}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "script")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "'math' is not available in Home Assistant templates" in res


def test_validate_blueprint_rejects_template_with_mutating_method(coordinator: Any) -> None:
    """Validate that Jinja2 templates calling mutating collection methods are rejected."""
    raw_yaml = """
blueprint:
  name: Mutating Method Script
  domain: script
  input: {}
sequence:
  - variables:
      val: "{{ [1, 2].append(3) }}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "script")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "calling mutating method '.append()' is not allowed" in res


def test_validate_blueprint_rejects_template_with_python_import(coordinator: Any) -> None:
    """Validate that Jinja2 templates attempting Python module imports are rejected."""
    raw_yaml = """
blueprint:
  name: Python Import Script
  domain: script
  input: {}
sequence:
  - variables:
      val: "{% import 'os' as os %}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "script")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "cannot import Python modules" in res


def test_validate_blueprint_rejects_template_with_missing_custom_template(coordinator: Any) -> None:
    """Validate that Jinja2 templates importing non-existent custom templates are rejected."""
    raw_yaml = """
blueprint:
  name: Missing Template Script
  domain: script
  input: {}
sequence:
  - variables:
      val: "{% from 'missing_macro.jinja' import my_macro %}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "script")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "custom template 'missing_macro.jinja' does not exist" in res


def test_validate_blueprint_allows_template_with_existing_custom_template(coordinator: Any) -> None:
    """Validate that Jinja2 templates importing existing custom templates are accepted."""
    from homeassistant.helpers.template import _get_hass_loader

    _get_hass_loader(coordinator.hass).sources["existing_macro.jinja"] = (
        "{% macro my_macro() %}hi{% endmacro %}"
    )
    raw_yaml = """
blueprint:
  name: Existing Template Script
  domain: script
  input: {}
sequence:
  - variables:
      val: "{% from 'existing_macro.jinja' import my_macro %}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "script")
    assert res is None


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_flags_missing_custom_template(
    hass: Any, coordinator: Any
) -> None:
    """Verify consumer validation flags missing custom templates as compatibility risks."""
    relative_path = "automation/missing_template.yaml"
    content = """
blueprint:
  name: Missing Template Automation
  domain: automation
  input: {}
trigger:
  - platform: time
    at: "00:00:00"
action:
  - variables:
      val: "{% from 'missing_macro.jinja' import my_macro %}"
"""
    risks = await coordinator._async_validate_blueprint_consumers(relative_path, content, {})
    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
    assert "missing_macro.jinja" in str(risks[0]["args"]["error"])


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_allows_existing_subdirectory_template(
    hass: Any, coordinator: Any
) -> None:
    """Verify consumer validation permits custom templates located in subdirectories."""
    from homeassistant.helpers.template import _get_hass_loader

    _get_hass_loader(hass).sources["my_lib/helpers.jinja"] = "{% macro test() %}1{% endmacro %}"

    relative_path = "automation/valid_subdir_template.yaml"
    content = """
blueprint:
  name: Valid Subdir Template Automation
  domain: automation
  input: {}
trigger:
  - platform: time
    at: "00:00:00"
action:
  - variables:
      val: "{% from 'my_lib/helpers.jinja' import test %}"
"""
    with patch.object(coordinator, "_async_validate_substituted_domain_config"):
        risks = await coordinator._async_validate_blueprint_consumers(relative_path, content, {})
    assert len(risks) == 0


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_with_mandatory_inputs(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation runs dummy inputs for mandatory blueprint inputs."""
    relative_path = "automation/mandatory_input.yaml"
    content = """
blueprint:
  name: Mandatory Input Automation
  domain: automation
  input:
    target_entity:
      name: Target Entity
      selector:
        entity:
          domain: light
trigger:
  - platform: state
    entity_id: !input target_entity
action:
  - action: light.turn_on
    target:
      entity_id: !input target_entity
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert risks == []
        assert mock_validate.await_args is not None
        call_config = mock_validate.await_args[1]["config"]
        assert isinstance(call_config, dict)
        triggers = call_config.get("triggers") or call_config.get("trigger")
        assert isinstance(triggers, list)
        assert triggers[0]["entity_id"] == "light.dummy"


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_preserves_defaults(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation preserves blueprint input defaults without dummy substitution."""
    relative_path = "automation/defaulted_inputs.yaml"
    content = """
blueprint:
  name: Defaulted Inputs Automation
  domain: automation
  input:
    motion_entity:
      name: Motion Entity
      default: "binary_sensor.kitchen_motion"
      selector:
        entity:
          domain: binary_sensor
    delay_seconds:
      name: Delay
      default: 30
      selector:
        number:
          min: 1
          max: 100
    mandatory_target:
      name: Mandatory Target
      selector:
        target: {}
trigger:
  - platform: state
    entity_id: !input motion_entity
action:
  - delay: !input delay_seconds
  - action: light.turn_on
    target: !input mandatory_target
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert risks == []
        assert mock_validate.await_args is not None
        call_config = mock_validate.await_args[1]["config"]
        assert isinstance(call_config, dict)
        triggers = call_config.get("triggers") or call_config.get("trigger")
        assert isinstance(triggers, list)
        # Default value is preserved
        assert triggers[0]["entity_id"] == "binary_sensor.kitchen_motion"
        actions = call_config.get("actions") or call_config.get("action")
        assert isinstance(actions, list)
        assert actions[0]["delay"] == 30
        assert actions[1]["target"] == {"entity_id": "test.dummy"}


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_multiple_and_diverse_selectors(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation handles multiple and diverse selector types."""
    relative_path = "automation/diverse_selectors.yaml"
    content = """
blueprint:
  name: Diverse Selectors Automation
  domain: automation
  input:
    target_lights:
      name: Target Lights
      selector:
        entity:
          multiple: true
    target_devices:
      name: Target Devices
      selector:
        device:
          multiple: true
    select_mode:
      name: Select Mode
      selector:
        select:
          options:
            - "bright"
            - "dim"
    threshold:
      name: Threshold
      selector:
        number:
          min: 5
          max: 100
trigger:
  - platform: state
    entity_id: !input target_lights
action:
  - action: light.turn_on
    target:
      device_id: !input target_devices
    data:
      mode: !input select_mode
      level: !input threshold
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert risks == []
        assert mock_validate.await_args is not None
        call_config = mock_validate.await_args[1]["config"]
        triggers = call_config.get("triggers") or call_config.get("trigger")
        assert triggers[0]["entity_id"] == ["test.dummy"]
        actions = call_config.get("actions") or call_config.get("action")
        assert actions[0]["target"]["device_id"] == ["dummy_device_id"]
        assert actions[0]["data"]["mode"] == "bright"
        assert actions[0]["data"]["level"] == 5.0


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_specialized_selectors(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation succeeds for blueprints with location and media selectors."""
    relative_path = "automation/specialized_selectors.yaml"
    content = """
blueprint:
  name: Specialized Selectors Automation
  domain: automation
  input:
    target_location:
      name: Target Location
      selector:
        location: {}
    target_media:
      name: Target Media
      selector:
        media: {}
trigger:
  - platform: state
    entity_id: test.dummy
action:
  - action: notify.notify
    data:
      location: !input target_location
      media: !input target_media
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert risks == []
        assert mock_validate.await_args is not None
        call_config = mock_validate.await_args[1]["config"]
        actions = call_config.get("actions") or call_config.get("action")
        assert actions[0]["data"]["location"] == {"latitude": 0.0, "longitude": 0.0}
        assert actions[0]["data"]["media"] == {
            "entity_id": "media_player.dummy",
            "media_content_id": "dummy",
            "media_content_type": "dummy",
        }


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_script_and_template_domains(
    hass: Any, coordinator: Any
) -> None:
    """Verify script and template blueprints run domain-specific baseline validation."""
    # Script domain
    script_path = "script/test_script.yaml"
    script_content = """
blueprint:
  name: Test Script BP
  domain: script
  input:
    delay_time:
      name: Delay
      default: 5
      selector:
        number:
          min: 1
          max: 60
sequence:
  - delay: !input delay_time
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_script_config",
        AsyncMock(),
    ) as mock_script_val:
        risks_script = await coordinator._async_validate_blueprint_consumers(
            script_path, script_content, configs={}
        )
        assert risks_script == []
        assert mock_script_val.await_args is not None
        call_script = mock_script_val.await_args[1]["config"]
        assert call_script.get("sequence") == [{"delay": 5}]

    # Template domain
    template_path = "template/test_template.yaml"
    template_content = """
blueprint:
  name: Test Template BP
  domain: template
  input:
    sensor_name:
      name: Sensor Name
      default: "Demo Sensor"
      selector:
        text: {}
sensor:
  - name: !input sensor_name
    state: "42"
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_template_config",
        AsyncMock(),
    ) as mock_tmpl_val:
        risks_tmpl = await coordinator._async_validate_blueprint_consumers(
            template_path, template_content, configs={}
        )
        assert risks_tmpl == []
        assert mock_tmpl_val.await_args is not None
        call_tmpl = mock_tmpl_val.await_args[1]["config"]
        assert call_tmpl.get("sensor") == [{"name": "Demo Sensor", "state": "42"}]


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_selectorless_derivable_inputs(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation succeeds for selectorless inputs with derivable shapes."""
    relative_path = "automation/selectorless_derivable.yaml"
    content = """
blueprint:
  name: Selectorless Derivable Automation
  domain: automation
  input:
    custom_target:
      name: Target to notify
    custom_action:
      name: Action to run
    custom_trigger:
      name: Trigger to wait for
trigger:
  - !input custom_trigger
action:
  - !input custom_action
  - action: light.turn_on
    target: !input custom_target
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert risks == []
        assert mock_validate.await_args is not None
        call_config = mock_validate.await_args[1]["config"]
        assert isinstance(call_config, dict)
        triggers = call_config.get("triggers") or call_config.get("trigger")
        assert isinstance(triggers, list)
        assert triggers[0].get("trigger") == "state" or triggers[0].get("platform") == "state"
        actions = call_config.get("actions") or call_config.get("action")
        assert isinstance(actions, list)
        assert actions[0]["action"] == "homeassistant.update_entity"
        assert actions[1]["target"] == {"entity_id": "test.dummy"}


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_selectorless_underivable_inputs(
    hass: Any, coordinator: Any
) -> None:
    """Verify baseline simulation skips validation when selectorless inputs cannot be derived."""
    relative_path = "automation/selectorless_underivable.yaml"
    content = """
blueprint:
  name: Selectorless Underivable Automation
  domain: automation
  input:
    opaque_param:
      name: Opaque Delay Setting
trigger:
  - platform: homeassistant
    event: start
action:
  - delay: !input opaque_param
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        # Excluded from baseline simulation without false-positive errors
        assert risks == []
        mock_validate.assert_not_called()


@pytest.mark.asyncio
async def test_async_validate_baseline_candidate_failure_records_compatibility_risk(
    hass: Any, coordinator: Any
) -> None:
    """Verify that a validation failure during baseline simulation records a COMPATIBILITY risk."""
    relative_path = "automation/failing_baseline.yaml"
    content = """
blueprint:
  name: Failing Baseline Automation
  domain: automation
  input:
    target_entity:
      name: Target Entity
      selector:
        entity:
          domain: light
trigger:
  - platform: state
    entity_id: !input target_entity
action:
  - action: light.turn_on
    target:
      entity_id: !input target_entity
"""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(side_effect=vol.Invalid("Invalid baseline substituted schema")),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(
            relative_path, content, configs={}
        )
        assert len(risks) == 1
        assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
        assert risks[0]["args"]["entity"] == relative_path
        assert "Invalid baseline substituted schema" in risks[0]["args"]["error"]


def test_validate_blueprint_rejects_unsafe_empty_default_in_trigger(coordinator: Any) -> None:
    """Validate that !input referencing an empty default inside trigger entity is rejected."""
    raw_yaml = """
blueprint:
  name: Unsafe Trigger Default
  domain: automation
  input:
    trigger_entity:
      name: Trigger Entity
      default: ""
      selector:
        entity: {}
trigger:
  - platform: state
    entity_id: !input trigger_entity
action:
  - action: light.turn_on
    target:
      entity_id: light.lamp
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "automation")
    assert res is not None
    assert "blueprint_validation_error" in res
    assert "trigger entity/device cannot default to an invalid value" in res


@pytest.mark.parametrize(
    "yaml_content",
    [
        # Direct top-level condition
        """
blueprint:
  name: Unsafe Condition Default
  domain: automation
  input:
    cond_entity:
      name: Cond Entity
      default: ""
      selector:
        entity: {}
trigger: []
condition:
  - condition: state
    entity_id: !input cond_entity
    state: "on"
action: []
""",
        # Nested in 'if' condition
        """
blueprint:
  name: Unsafe If Default
  domain: automation
  input:
    cond_entity:
      name: Cond Entity
      default: ""
      selector:
        entity: {}
trigger: []
action:
  - if:
      - condition: state
        entity_id: !input cond_entity
        state: "on"
    then: []
""",
        # Nested in 'while' condition
        """
blueprint:
  name: Unsafe While Default
  domain: automation
  input:
    cond_entity:
      name: Cond Entity
      default: ""
      selector:
        entity: {}
trigger: []
action:
  - repeat:
      while:
        - condition: state
          entity_id: !input cond_entity
          state: "on"
      sequence: []
""",
        # Nested in 'until' condition
        """
blueprint:
  name: Unsafe Until Default
  domain: automation
  input:
    cond_entity:
      name: Cond Entity
      default: ""
      selector:
        entity: {}
trigger: []
action:
  - repeat:
      until:
        - condition: state
          entity_id: !input cond_entity
          state: "on"
      sequence: []
""",
    ],
    ids=["top_level_condition", "if_condition", "while_condition", "until_condition"],
)
def test_validate_blueprint_rejects_unsafe_empty_default_in_conditions(
    coordinator: Any, yaml_content: str
) -> None:
    """Validate that empty-default inputs inside condition blocks are rejected."""
    data = yaml_util.parse_yaml(yaml_content)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "automation")
    assert res is not None
    assert "condition entity/device cannot default" in res


def test_validate_blueprint_allows_empty_default_in_variables(coordinator: Any) -> None:
    """Validate that empty-default inputs referenced in variables blocks remain allowed."""
    raw_yaml = """
blueprint:
  name: Allowed Variable References
  domain: automation
  input:
    opt_filter:
      name: Optional Filter
      default: ""
      selector:
        text: {}
    opt_entity:
      name: Optional Entity
      default: ""
      selector:
        entity: {}
trigger_variables:
  tv_filter: !input opt_filter
variables:
  v_filter: !input opt_filter
  v_entity: !input opt_entity
trigger:
  - platform: homeassistant
    event: start
action:
  - action: light.turn_on
    target:
      entity_id: light.safe_lamp
    data:
      message: "{{ v_filter or 'fallback' }}"
"""
    data = yaml_util.parse_yaml(raw_yaml)
    assert isinstance(data, dict)
    res = coordinator._validate_blueprint(data, "https://example.com/bp.yaml", "automation")
    assert res is None
