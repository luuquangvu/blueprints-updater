"""Tests for coordinator behavior during blueprint validation and hub interactions.

This module provides focused testing for the coordination logic between the
blueprints_updater integration and the Home Assistant blueprint hub, ensuring
robust fail-safe mechanisms are in place during compatibility checks.
"""

import asyncio
import os
import socket
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import DOMAIN_AUTOMATION, BlueprintRiskType
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

    hass.data["blueprint"] = {DOMAIN_AUTOMATION: mock_hub}

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
    assert coordinator.data[path2]["last_error"].startswith("yaml_syntax_error|")

    path3 = "automation/schema.yaml"
    coordinator.data[path3] = dict(info)
    with patch(
        "custom_components.blueprints_updater.coordinator.Blueprint",
        side_effect=InvalidBlueprint(DOMAIN_AUTOMATION, "test", {}, "Mock Schema Failure"),
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
        assert coordinator.data[path3]["last_error"].startswith("blueprint_validation_error|")
        assert "Mock Schema Failure" in coordinator.data[path3]["last_error"]


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
    """Test that filter mode is normalized (lowercase and stripped)."""
    assert get_validated_filter_mode("  All  ") == "all"
    assert get_validated_filter_mode("WHITELIST") == "whitelist"
    assert get_validated_filter_mode("Blacklist") == "blacklist"
    assert get_validated_filter_mode("invalid") == "all"
    assert get_validated_filter_mode(None) == "all"
    assert get_validated_filter_mode(123) == "all"


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
    expected = coordinator._normalize_content(content)
    assert coordinator._ensure_source_url(content, source_url) == expected


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_voluptuous_error(hass, coordinator):
    """Verify that voluptuous validation failures are registered as COMPATIBILITY risks."""
    relative_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"

    mock_hub = MagicMock()
    mock_hub._blueprints = {}
    hass.data["blueprint"] = {DOMAIN_AUTOMATION: mock_hub}

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
