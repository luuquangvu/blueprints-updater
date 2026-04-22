"""Tests for coordinator update cycle, scanning, and refresh logic."""

import asyncio
import contextlib
import hashlib
import os
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, mock_open, patch
from urllib.parse import urlparse

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util

from custom_components.blueprints_updater.const import (
    CONF_USE_CDN,
    DOMAIN_JSDELIVR,
    FILTER_MODE_ALL,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.mark.asyncio
async def test_scan_blueprints(coordinator):
    """Test scanning for blueprints on disk."""
    base_path = "C:/config/blueprints" if os.name == "nt" else "/config/blueprints"
    auto_path = os.path.normpath(os.path.join(base_path, "automation"))
    script_path = os.path.normpath(os.path.join(base_path, "script"))

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._get_entities_using_blueprint_list",
            return_value=["automation.test"],
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.os.walk",
            return_value=[
                (auto_path, [], ["test.yaml"]),
                (script_path, [], ["script1.yaml"]),
            ],
        ),
        patch("custom_components.blueprints_updater.coordinator.os.path.isdir", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.open",
            side_effect=[
                mock_open(
                    read_data="blueprint:\n  name: Test\n  domain: automation\n  source_url: https://example.com/blueprint1.yaml\n"
                ).return_value,
                mock_open(
                    read_data="blueprint:\n  name: Script\n  domain: script\n  source_url: https://example.com/blueprint2.yaml\n"
                ).return_value,
            ],
        ),
        patch.object(coordinator.hass.config, "path", return_value=base_path),
    ):
        blueprints = BlueprintUpdateCoordinator.scan_blueprints(
            coordinator.hass, FILTER_MODE_ALL, []
        )

    path_test = os.path.normpath(os.path.join(auto_path, "test.yaml"))

    assert len(blueprints) == 2
    assert path_test in blueprints
    assert blueprints[path_test]["name"] == "Test"
    assert blueprints[path_test]["domain"] == "automation"


@pytest.mark.asyncio
async def test_scan_blueprints_domain_extraction(coordinator):
    """Test that domain is extracted correctly from folder structure during scan."""
    base_path = "C:/config/blueprints" if os.name == "nt" else "/config/blueprints"
    auto_path = os.path.normpath(os.path.join(base_path, "automation"))
    script_path = os.path.normpath(os.path.join(base_path, "script"))
    nested_path = os.path.normpath(os.path.join(base_path, "nested/deep/automation"))

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.walk",
            return_value=[
                (auto_path, [], ["a.yaml"]),
                (script_path, [], ["s.yaml"]),
                (nested_path, [], ["d.yaml"]),
            ],
        ),
        patch("custom_components.blueprints_updater.coordinator.os.path.isdir", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.open",
            side_effect=[
                mock_open(
                    read_data="blueprint:\n  name: Test\n  source_url: https://example.com/blueprint1.yaml\n"
                ).return_value,
                mock_open(
                    read_data="blueprint:\n  name: Test\n  source_url: https://example.com/blueprint2.yaml\n"
                ).return_value,
                mock_open(
                    read_data="blueprint:\n  name: Test\n  source_url: https://example.com/blueprint3.yaml\n"
                ).return_value,
            ],
        ),
        patch.object(coordinator.hass.config, "path", return_value=base_path),
    ):
        blueprints = BlueprintUpdateCoordinator.scan_blueprints(
            coordinator.hass, FILTER_MODE_ALL, []
        )

    path_a = os.path.normpath(os.path.join(auto_path, "a.yaml"))
    path_s = os.path.normpath(os.path.join(script_path, "s.yaml"))
    path_d = os.path.normpath(os.path.join(nested_path, "d.yaml"))

    assert blueprints[path_a]["domain"] == "automation"
    assert blueprints[path_s]["domain"] == "script"
    assert blueprints[path_d]["domain"] == "automation"


@pytest.mark.asyncio
async def test_async_fetch_blueprint_force(coordinator):
    """Test fetching a single blueprint with force=True."""
    path = "/config/blueprints/automation/test.yaml"
    url = "https://example.com/blueprint.yaml"
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": url,
            "local_hash": "old_hash",
            "etag": "old_etag",
        }
    }

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = f"blueprint:\n  name: New\n  domain: automation\n  source_url: {url}\n"
    mock_response.headers = httpx.Headers({"ETag": "new_etag", "Content-Type": "text/yaml"})
    mock_response.raise_for_status = MagicMock()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=mock_session,
        ),
        patch.object(coordinator, "_execute_with_redirect_guard", return_value=mock_response),
    ):
        await coordinator.async_fetch_blueprint(path, force=True)

    assert coordinator.data[path]["etag"] == "new_etag"
    assert coordinator.data[path]["remote_hash"] is not None


@pytest.mark.asyncio
async def test_async_update_data_partial_failure(coordinator):
    """Test that partial failures don't stop the entire update."""
    path1 = "/config/blueprints/automation/ok.yaml"
    path2 = "/config/blueprints/automation/fail.yaml"
    url1 = "https://example.com/blueprint1.yaml"
    url2 = "https://example.com/blueprint2.yaml"

    blueprints = {
        path1: {
            "name": "OK",
            "rel_path": "automation/ok.yaml",
            "domain": "automation",
            "source_url": url1,
            "local_hash": "h1",
        },
        path2: {
            "name": "Fail",
            "rel_path": "automation/fail.yaml",
            "domain": "automation",
            "source_url": url2,
            "local_hash": "h2",
        },
    }

    coordinator._async_fetch_with_cdn_fallback = AsyncMock(
        side_effect=[
            (f"blueprint:\n  name: OK\n  domain: automation\n  source_url: {url1}\n", "e1"),
            httpx.RequestError("Failed"),
        ]
    )

    with (
        patch.object(BlueprintUpdateCoordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()
        await coordinator._async_background_refresh(blueprints)
        results = coordinator.data

    assert results[path1]["last_error"] is None
    assert results[path2]["last_error"] is not None


@pytest.mark.asyncio
async def test_async_background_refresh_503_resilience(coordinator):
    """Test resilience to 503 errors during background refresh."""
    path = "automation/test.yaml"
    coordinator.data = {
        path: {"source_url": "https://example.com/blueprint.yaml", "local_hash": "h"}
    }

    coordinator._async_fetch_with_cdn_fallback = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Service Unavailable",
            request=MagicMock(),
            response=MagicMock(status_code=503),
        )
    )

    with patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn:
        await coordinator._async_update_blueprint_in_place(
            MagicMock(), path, coordinator.data[path], [], set()
        )
        mock_warn.assert_any_call(
            "Failed to fetch blueprint from (redacted URL): %s", "Service Unavailable"
        )


@pytest.mark.asyncio
@patch.object(BlueprintUpdateCoordinator, "async_translate", return_value="Mocked Translation")
async def test_async_update_data_auto_update(mock_translate, coordinator):
    """Test automatic update logic."""
    path = "/config/blueprints/automation/test.yaml"
    url = "https://example.com/blueprint.yaml"
    coordinator.config_entry.options = {"auto_update": True}
    content = f"blueprint:\n  name: New\n  domain: automation\n  source_url: {url}\n"
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": url,
            "local_hash": "old",
            "remote_hash": "new",
            "remote_content": content,
            "updatable": True,
        }
    }

    coordinator.async_install_blueprint = AsyncMock()
    coordinator._async_fetch_with_cdn_fallback = AsyncMock(return_value=(content, "new_etag"))

    with (
        patch.object(
            BlueprintUpdateCoordinator,
            "scan_blueprints",
            side_effect=lambda *args: coordinator.data,
        ),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()
        await coordinator._async_background_refresh(coordinator.data)

    coordinator.async_install_blueprint.assert_awaited_once_with(
        path, content, reload_services=False, backup=True
    )


@pytest.mark.asyncio
@patch.object(BlueprintUpdateCoordinator, "async_translate", return_value="Mocked Translation")
async def test_async_update_data_auto_update_multiple_sorted(mock_translate, coordinator):
    """Test auto-update handles multiple blueprints in correct order."""
    coordinator.config_entry.options = {"auto_update": True}
    u1 = "https://example.com/blueprint1.yaml"
    u2 = "https://example.com/blueprint2.yaml"
    c1 = f"blueprint:\n  name: A\n  domain: automation\n  source_url: {u1}\n"
    c2 = f"blueprint:\n  name: B\n  domain: automation\n  source_url: {u2}\n"
    coordinator.data = {
        "path1": {
            "name": "A",
            "rel_path": "automation/a.yaml",
            "domain": "automation",
            "source_url": u1,
            "updatable": True,
            "remote_content": c1,
            "remote_hash": "h1",
            "local_hash": "l1",
        },
        "path2": {
            "name": "B",
            "rel_path": "automation/b.yaml",
            "domain": "automation",
            "source_url": u2,
            "updatable": True,
            "remote_content": c2,
            "remote_hash": "h2",
            "local_hash": "l2",
        },
    }

    coordinator.async_install_blueprint = AsyncMock()
    coordinator._async_fetch_with_cdn_fallback = AsyncMock(side_effect=[(c2, "e2"), (c1, "e1")])

    with (
        patch.object(
            BlueprintUpdateCoordinator,
            "scan_blueprints",
            side_effect=lambda *args: coordinator.data,
        ),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()
        await coordinator._async_background_refresh(coordinator.data)

    call_paths = [call.args[0] for call in coordinator.async_install_blueprint.call_args_list]
    assert set(call_paths) == {"path1", "path2"}
    assert len(call_paths) == 2


@pytest.mark.asyncio
async def test_async_update_blueprint_unsafe_url_invalidates_cache(coordinator):
    """Test that switching to an unsafe URL invalidates previous cache."""
    path = "automation/test.yaml"
    coordinator.data[path] = {
        "source_url": "https://unsafe.com/bp.yaml",
        "etag": "old_etag",
        "remote_hash": "old_hash",
        "updatable": True,
    }

    coordinator._is_safe_url = AsyncMock(return_value=False)

    await coordinator._async_update_blueprint_in_place(
        MagicMock(), path, coordinator.data[path], [], set()
    )

    assert coordinator.data[path]["etag"] is None
    assert coordinator.data[path]["remote_hash"] is None
    assert coordinator.data[path]["updatable"] is False
    assert "unsafe_url" in coordinator.data[path]["last_error"]


@pytest.mark.asyncio
async def test_async_background_refresh_concurrency_and_cancellation(hass, coordinator):
    """Test that background refresh respects concurrency limits and handles cancellation."""
    num_blueprints = MAX_CONCURRENT_REQUESTS * 2
    paths = [f"path{i}" for i in range(num_blueprints)]
    for p in paths:
        coordinator.data[p] = {"source_url": "https://example.com", "local_hash": "h"}

    active_workers = 0
    max_observed_concurrency = 0
    processed_count = 0

    worker_started_event = asyncio.Event()
    block_event = asyncio.Event()

    async def mock_update_in_place(*args, **kwargs):
        nonlocal active_workers, max_observed_concurrency, processed_count
        active_workers += 1
        max_observed_concurrency = max(max_observed_concurrency, active_workers)

        worker_started_event.set()

        try:
            await block_event.wait()
            processed_count += 1
        finally:
            active_workers -= 1

    coordinator._async_update_blueprint_in_place = AsyncMock(side_effect=mock_update_in_place)

    refresh_task = asyncio.create_task(coordinator._async_background_refresh(coordinator.data))
    await worker_started_event.wait()

    for _ in range(10):
        await asyncio.sleep(0)

    assert max_observed_concurrency <= MAX_CONCURRENT_REQUESTS
    assert active_workers == MAX_CONCURRENT_REQUESTS

    refresh_task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await refresh_task
    block_event.set()

    await asyncio.sleep(0)

    assert processed_count < num_blueprints
    assert active_workers == 0


@pytest.mark.asyncio
async def test_async_fetch_blueprint_regression_key_error_hash(coordinator):
    """Regression test for KeyError when updating a blueprint not in coordinator.data."""
    path = "/config/blueprints/automation/new.yaml"
    url = "https://example.com/new_blueprint.yaml"
    content = f"blueprint:\n  name: New\n  domain: automation\n  source_url: {url}\n"

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = content
    mock_response.headers = httpx.Headers({"ETag": "etag", "Content-Type": "text/yaml"})
    mock_response.raise_for_status = MagicMock()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=mock_session,
        ),
        patch.object(coordinator, "_execute_with_redirect_guard", return_value=mock_response),
        patch.object(
            BlueprintUpdateCoordinator,
            "scan_blueprints",
            return_value={
                path: {
                    "local_hash": "h",
                    "name": "N",
                    "rel_path": "P",
                    "domain": "automation",
                    "source_url": url,
                }
            },
        ),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()
        await coordinator.async_fetch_blueprint(path)

    assert path in coordinator.data
    assert coordinator.data[path]["remote_hash"] is not None


@pytest.mark.asyncio
async def test_ghost_update_prevention(coordinator):
    """Test that updates are rejected if remote content matches local file but not remote_hash."""
    path = "/config/blueprints/automation/test.yaml"
    url = "https://github.com/user/repo/blob/main/test.yaml"
    content = f"blueprint:\n  name: Test\n  domain: automation\n  source_url: {url}\n"
    local_hash = hashlib.sha256(content.encode()).hexdigest()

    coordinator.data[path] = {
        "local_hash": local_hash,
        "remote_hash": "different_hash",
        "updatable": True,
    }

    results_to_notify = []
    updated_domains = set()

    with (
        patch("custom_components.blueprints_updater.coordinator._LOGGER.debug"),
        patch.object(coordinator, "_async_save_metadata"),
    ):
        await coordinator._process_blueprint_content(
            path,
            coordinator.data[path],
            content,
            "new_etag",
            url,
            results_to_notify,
            updated_domains,
        )

        assert coordinator.data[path]["updatable"] is False
        assert coordinator.data[path]["remote_hash"] == local_hash
        assert coordinator.data[path]["etag"] == "new_etag"


@pytest.mark.asyncio
async def test_yaml_normalization_ignores_comments(coordinator):
    """Test that adding/changing comments does NOT trigger an update."""
    path = "/config/blueprints/automation/test.yaml"
    url = "https://github.com/user/repo/blob/main/test.yaml"
    content = f"blueprint:\n  name: Test\n  domain: automation\n  source_url: {url}\n"

    canonical_content = yaml_util.dump(cast(dict[str, Any], yaml_util.parse_yaml(content)))
    local_hash = hashlib.sha256(canonical_content.encode()).hexdigest()

    coordinator.data[path] = {
        "local_hash": local_hash,
        "remote_hash": local_hash,
        "updatable": False,
    }

    new_content = f"{content}# new comment line\n"
    await coordinator._process_blueprint_content(
        path, coordinator.data[path], new_content, "e", url, [], set()
    )
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path]["remote_hash"] == local_hash


@pytest.mark.asyncio
async def test_handle_source_url_change_clears_metadata(coordinator):
    """Test that changing source_url clears old ETags and remote hashes."""
    path = "automation/test.yaml"
    coordinator._persisted_etags = {path: "old_etag"}
    coordinator._persisted_hashes = {path: "old_hash"}
    coordinator.data = {
        path: {
            "source_url": "https://example.com/old.yaml",
            "etag": "old_etag",
            "remote_hash": "old_hash",
        }
    }

    new_info = {"source_url": "https://example.com/new_blueprint.yaml", "local_hash": "h"}
    result = coordinator._handle_source_url_change(path, new_info, coordinator.data[path])

    assert result.get("etag") is None
    assert result.get("remote_hash") is None


@pytest.mark.asyncio
async def test_process_blueprint_content_yaml_error(coordinator):
    """Test handling of YAML syntax errors during content processing."""
    path = "automation/error.yaml"
    info = {"rel_path": "automation/error.yaml", "name": "Error", "local_hash": "h"}
    coordinator.data[path] = info

    await coordinator._process_blueprint_content(
        path, info, "invalid: yaml: [data", "etag", "https://example.com/blueprint.yaml", [], set()
    )

    assert coordinator.data[path]["last_error"].startswith("yaml_syntax_error|")


@pytest.mark.asyncio
async def test_process_blueprint_content_unhandled_error(coordinator):
    """Test handling of unexpected errors during content processing."""
    path = "automation/error.yaml"
    info = {"rel_path": "automation/error.yaml", "name": "Error", "local_hash": "h"}
    coordinator.data[path] = info

    with patch.object(coordinator, "_ensure_source_url", side_effect=RuntimeError("Boom")):
        await coordinator._process_blueprint_content(
            path,
            info,
            "blueprint: { name: Test }",
            "etag",
            "https://example.com/blueprint.yaml",
            [],
            set(),
        )

    assert coordinator.data[path]["last_error"] == "processing_error|Boom"


@pytest.mark.asyncio
async def test_detect_risks_system_error_on_exception(coordinator):
    """Test that exceptions during risk detection result in a system_error risk."""
    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"
    info = {"rel_path": rel_path, "name": "Test Blueprint"}
    coordinator.data = {path: info}

    remote_content = "blueprint:\n  name: New\n  domain: automation\n  source_url: https://example.com/new_blueprint.yaml\n"

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.open",
            side_effect=Exception("Test Exception"),
        ),
    ):
        risks = await coordinator._detect_risks_for_update(path, info, remote_content, None)

    assert len(risks) == 1
    assert risks[0]["type"] == "system_error"
    assert risks[0]["args"]["error"] == "Test Exception"
    assert risks[0]["args"]["path"] == rel_path


@pytest.mark.asyncio
async def test_detect_risks_missing_rel_path(coordinator):
    """Test that missing rel_path results in a system_error risk."""
    path = "/config/blueprints/automation/test.yaml"
    info = {"name": "Test Blueprint"}
    coordinator.data = {path: info}

    risks = await coordinator._detect_risks_for_update(path, info, "content", None)

    assert len(risks) == 1
    assert risks[0]["type"] == "system_error"
    assert risks[0]["args"]["error"] == "missing_path"
    assert risks[0]["args"]["path"] == "test.yaml"


@pytest.mark.asyncio
async def test_async_install_blueprint(hass, coordinator):
    """Test installing a blueprint and reloading services."""
    path = "/config/blueprints/automation/test.yaml"
    remote_content = "blueprint:\n  name: Test"

    hass.services.has_service = MagicMock(
        side_effect=lambda domain, service: (
            domain in ["automation", "script"] if service == "reload" else False
        )
    )
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
    ):
        await coordinator.async_install_blueprint(path, remote_content)

    assert hass.services.async_call.call_count == 1
    hass.services.async_call.assert_any_call("automation", "reload")

    with pytest.raises(AssertionError):
        hass.services.async_call.assert_any_call("template", "reload")


@pytest.mark.asyncio
async def test_async_install_blueprint_backup(hass, coordinator):
    """Test installing a blueprint with backup enabled."""
    path = "/config/blueprints/automation/test.yaml"
    remote_content = "blueprint:\n  name: Test"

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 3})
    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.realpath",
            side_effect=os.path.normpath,
        ),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2") as mock_copy,
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
    ):
        await coordinator.async_install_blueprint(path, remote_content, backup=True)

    mock_copy.assert_called_once_with(os.path.normpath(path), os.path.normpath(f"{path}.bak.1"))
    mock_replace.assert_any_call(os.path.normpath(f"{path}.tmp"), os.path.normpath(path))


@pytest.mark.asyncio
async def test_async_install_blueprint_domain_normalization(hass, coordinator):
    """Test that async_install_blueprint correctly normalizes the domain."""
    path = "/config/blueprints/automation/test.yaml"

    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
    ):
        content_domain = "blueprint:\n  name: Test\n  domain:  script  "
        await coordinator.async_install_blueprint(path, content_domain)
        hass.services.async_call.assert_called_once_with("script", "reload")
        hass.services.async_call.reset_mock()
        content_no_domain = "blueprint:\n  name: Test"
        await coordinator.async_install_blueprint(path, content_no_domain)
        hass.services.async_call.assert_called_once_with("automation", "reload")
        hass.services.async_call.reset_mock()
        content_empty_domain = "blueprint:\n  name: Test\n  domain: ''"
        await coordinator.async_install_blueprint(path, content_empty_domain)
        hass.services.async_call.assert_called_once_with("automation", "reload")

        hass.services.async_call.reset_mock()
        with patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger:
            content_invalid_domain = "blueprint:\n  name: Test\n  domain:  unknown_domain  "
            await coordinator.async_install_blueprint(path, content_invalid_domain)
            hass.services.async_call.assert_called_once_with("automation", "reload")
            mock_logger.warning.assert_called()
            assert "unknown_domain" in mock_logger.warning.call_args[0][1]


@pytest.mark.asyncio
async def test_async_install_blueprint_error(coordinator):
    """Test exception during blueprint installation."""
    with (
        patch("builtins.open", side_effect=Exception("Write failed")),
        pytest.raises(Exception, match="Write failed"),
    ):
        await coordinator.async_install_blueprint("/fake/path.yaml", "content")


@pytest.mark.asyncio
async def test_async_install_blueprint_reload_fallback(coordinator):
    """Test that reload fallback works when blueprint block is missing or malformed."""
    path = "automation/test.yaml"
    content = "invalid: yaml"

    coordinator.async_reload_services = AsyncMock()
    coordinator._async_save_metadata = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
    ):
        await coordinator.async_install_blueprint(path, content, reload_services=True)
    coordinator.async_reload_services.assert_called_once_with(["automation"])
    coordinator._async_save_metadata.assert_not_called()

    coordinator.async_reload_services.reset_mock()
    coordinator._async_save_metadata.reset_mock()
    coordinator.data = {path: {"domain": "script", "name": "Test"}}
    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
    ):
        await coordinator.async_install_blueprint(path, content, reload_services=True)
    coordinator.async_reload_services.assert_called_once_with(["script"])
    coordinator._async_save_metadata.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_async_install_blueprint_state_sync_fix(coordinator):
    """Test that async_install_blueprint syncs hashes and triggers UI update."""
    path = "/config/blueprints/automation/test.yaml"
    raw_remote = "blueprint:\r\n  name: New\r\n"
    coordinator.data = {
        path: {
            "local_hash": "old",
            "remote_hash": "new",
            "invalid_remote_hash": "bad",
            "last_error": "error",
            "remote_content": "old_remote",
            "updatable": True,
        }
    }

    expected_hash = coordinator._hash_content(raw_remote)

    mock_open_obj = mock_open()
    with (
        patch("builtins.open", mock_open_obj),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch.object(coordinator, "async_reload_services"),
    ):
        await coordinator.async_install_blueprint(path, raw_remote)

    assert coordinator.data[path]["local_hash"] == expected_hash
    assert coordinator.data[path]["remote_hash"] == expected_hash
    assert not coordinator.data[path]["updatable"]
    assert coordinator.data[path]["last_error"] is None
    assert coordinator.data[path]["invalid_remote_hash"] is None
    assert coordinator.data[path]["remote_content"] is None
    coordinator.async_set_updated_data.assert_called_with(coordinator.data)

    mock_open_obj().write.assert_called()
    written_data = "".join(call.args[0] for call in mock_open_obj().write.call_args_list)
    assert "name: New" in written_data

    assert mock_replace.called
    assert os.path.normpath(mock_replace.call_args[0][1]).endswith(os.path.normpath(path))


@pytest.mark.asyncio
async def test_async_install_blueprint_state_synchronization(coordinator):
    """Test that self.data is updated immediately after async_install_blueprint."""
    path = "/config/blueprints/automation/test.yaml"
    remote_content = "blueprint:\n  name: New Version\n  source_url: https://url\n"
    new_hash = hashlib.sha256(remote_content.encode()).hexdigest()

    coordinator.data = {
        path: {
            "name": "Old",
            "rel_path": "automation/test.yaml",
            "local_hash": "old_hash",
            "remote_hash": new_hash,
            "updatable": True,
        }
    }

    with (
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch("builtins.open", mock_open()),
        patch.object(coordinator, "async_reload_services", new_callable=AsyncMock),
    ):
        await coordinator.async_install_blueprint(
            path, remote_content, reload_services=False, backup=False
        )

    assert coordinator.data[path]["local_hash"] == new_hash
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path]["last_error"] is None


@pytest.mark.asyncio
async def test_async_install_blueprint_targeted_reload(coordinator):
    """Test that installing a blueprint with a specific domain only reloads that domain."""
    path = "/config/blueprints/automation/script.yaml"
    content = "blueprint:\n  name: Test Script\n  domain: script"

    coordinator.hass.services.has_service = MagicMock(return_value=True)
    coordinator.hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
    ):
        await coordinator.async_install_blueprint(path, content)

    coordinator.hass.services.async_call.assert_called_once_with("script", "reload")


@pytest.mark.asyncio
async def test_async_install_blueprint_unsafe_path(coordinator):
    """Test that installing to an unsafe path is blocked."""
    coordinator._is_safe_path = BlueprintUpdateCoordinator._is_safe_path.__get__(coordinator)
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.realpath",
            side_effect=os.path.normpath,
        ),
        patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger,
    ):
        with pytest.raises(
            HomeAssistantError,
            match=r"Security violation: Attempted to install to an unsafe location",
        ):
            await coordinator.async_install_blueprint("/config/secrets.yaml", "content")
        mock_logger.error.assert_called_with(
            "Security violation: Attempted to install to unsafe path: %s",
            os.path.normpath("/config/secrets.yaml"),
        )


@pytest.mark.asyncio
async def test_async_install_blueprint_yaml_error_logging(coordinator):
    """Test that YAML errors during install reload are logged as warnings."""
    path = "/config/blueprints/automation/test.yaml"
    content = "invalid yaml"

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch(
            "custom_components.blueprints_updater.coordinator.yaml_util.parse_yaml",
            side_effect=HomeAssistantError("Parsing failed"),
        ),
        patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger,
    ):
        await coordinator.async_install_blueprint(path, content, reload_services=True)

    mock_logger.warning.assert_called_with("Failed to parse blueprint at %s: %s", path, ANY)


@pytest.mark.asyncio
async def test_async_update_blueprint(coordinator):
    """Test the full update flow for a single blueprint."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "domain": "automation",
        "local_hash": "old_hash",
    }
    results: dict[str, Any] = {path: {"last_error": None, "local_hash": "old_hash"}}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_etag", "Content-Type": "text/yaml"}
    mock_response.raise_for_status = MagicMock()
    mock_response.text = "blueprint:\n  name: Test"

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    with (
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_hash.return_value.hexdigest.return_value = "new_hash"
        coordinator.data = results
        results_to_notify = []
        updated_domains = set()
        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify, updated_domains
        )

    assert path in results
    assert results[path]["updatable"] is True
    assert results[path]["remote_hash"] == "new_hash"
    assert results[path]["etag"] == "new_etag"
    assert "source_url" in results[path]["remote_content"]

    kwargs = mock_session.get.call_args.kwargs
    assert kwargs["timeout"] == REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_async_update_blueprint_304_auto_update(coordinator):
    """Test that auto-update works even if the fetch returns 304."""
    path = "/config/blueprints/automation/test.yaml"
    source_url = "https://url/test.yaml"

    coordinator.config_entry.options = MappingProxyType({"auto_update": True})

    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "source_url": source_url,
            "local_hash": "old_hash",
            "updatable": True,
            "remote_hash": "new_hash",
            "etag": "stored_etag",
            "remote_content": None,
        }
    }

    mock_response_304 = MagicMock(spec=httpx.Response)
    mock_response_304.status_code = 304
    mock_response_304.headers = {"ETag": "stored_etag"}

    mock_response_200 = MagicMock(spec=httpx.Response)
    mock_response_200.status_code = 200
    mock_response_200.headers = {"ETag": "stored_etag", "Content-Type": "text/yaml"}
    mock_response_200.text = "blueprint:\n  name: Test\n  source_url: https://url/test.yaml"
    mock_response_200.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=[mock_response_304, mock_response_200])

    with (
        patch.object(
            coordinator, "async_install_blueprint", new_callable=AsyncMock
        ) as mock_install,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch.object(coordinator, "_is_safe_url", AsyncMock(return_value=True)),
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
    ):
        mock_hash.return_value.hexdigest.return_value = "new_hash"

        info = coordinator.data[path]
        results_to_notify = []
        updated_domains = set()

        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify, updated_domains
        )

        mock_install.assert_called_once()
        assert mock_session.get.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cdn_config, expect_cdn",
    [
        (None, True),
        (True, True),
        (False, False),
    ],
)
async def test_async_update_blueprint_cdn_gating(coordinator, cdn_config, expect_cdn):
    """Test that cdn_url is only passed to fetcher based on config gating."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "domain": "automation",
        "local_hash": "old_hash",
    }
    coordinator.data = {path: info}

    if cdn_config is None:
        coordinator.config_entry.options = MappingProxyType({})
    else:
        coordinator.config_entry.options = MappingProxyType({CONF_USE_CDN: cdn_config})

    mock_session = MagicMock(spec=httpx.AsyncClient)
    results_to_notify = []
    updated_domains = set()

    with patch.object(
        coordinator, "_async_fetch_with_cdn_fallback", AsyncMock(return_value=("cont", "etag"))
    ) as mock_fetch:
        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify, updated_domains
        )
        _args, _kwargs = mock_fetch.call_args
        cdn_url_arg = _args[3]

        if expect_cdn:
            assert cdn_url_arg is not None
            parsed = urlparse(cdn_url_arg)
            assert parsed.hostname == DOMAIN_JSDELIVR
            assert parsed.scheme == "https"
        else:
            assert cdn_url_arg is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_case", ["unsafe_url", "fetch_error", "empty_content", "processing_error"]
)
async def test_async_update_blueprint_failure_paths(coordinator, error_case):
    """Test that _async_update_blueprint_in_place handles failure paths correctly."""
    path = f"test_{error_case}.yaml"
    info = {
        "source_url": "https://raw.githubusercontent.com/u/r/b/p.yaml",
        "name": f"Error {error_case}",
    }
    coordinator.data[path] = {
        "remote_hash": "old",
        "etag": "old-etag",
        "invalid_remote_hash": "stale",
    }

    if error_case == "unsafe_url":
        info["source_url"] = "https://malicious.com/exploit.yaml"
        coordinator._is_safe_url = AsyncMock(return_value=False)
    elif error_case == "fetch_error":
        coordinator._async_fetch_with_cdn_fallback = AsyncMock(
            side_effect=httpx.HTTPError("Network down")
        )
    elif error_case == "empty_content":
        coordinator._async_fetch_with_cdn_fallback = AsyncMock(return_value=("", "new-etag"))
    elif error_case == "processing_error":
        coordinator._async_fetch_with_cdn_fallback = AsyncMock(
            return_value=("valid content", "new-etag")
        )
        coordinator._process_blueprint_content = AsyncMock(
            side_effect=ValueError("Invalid structure")
        )

    await coordinator._async_update_blueprint_in_place(
        MagicMock(spec=httpx.AsyncClient), path, info, [], set()
    )

    entry = coordinator.data[path]
    assert entry["last_error"].startswith(f"{error_case}|")
    assert entry["updatable"] is False
    assert entry["remote_hash"] is None
    assert entry["remote_content"] is None

    if error_case == "fetch_error":
        assert entry["etag"] == "old-etag"
    else:
        assert entry["etag"] is None

    if error_case == "empty_content":
        assert entry["invalid_remote_hash"] is None


@pytest.mark.asyncio
async def test_async_update_blueprint_in_place_errors(coordinator):
    """Test various error conditions in _async_update_blueprint_in_place."""
    path = "/config/blueprints/automation/test.yaml"
    info = {"name": "Test", "source_url": "https://url", "local_hash": "hash"}
    results = {
        path: {
            "last_error": None,
            "local_hash": "hash",
            "name": "Test",
            "source_url": "https://url",
        }
    }
    coordinator.data = results

    mock_resp_empty = MagicMock(spec=httpx.Response)
    mock_resp_empty.status_code = 200
    mock_resp_empty.headers = {"Content-Type": "text/yaml"}
    mock_resp_empty.raise_for_status = MagicMock()
    mock_resp_empty.text = ""

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_resp_empty)
    results_to_notify = []
    updated_domains = set()

    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )
    assert coordinator.data[path]["last_error"] == "empty_content|"
    assert coordinator.data[path]["remote_hash"] is None
    assert coordinator.data[path]["remote_content"] is None
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path]["invalid_remote_hash"] is None

    mock_resp_invalid = MagicMock(spec=httpx.Response)
    mock_resp_invalid.status_code = 200
    mock_resp_invalid.headers = {"Content-Type": "text/yaml"}
    mock_resp_invalid.raise_for_status = MagicMock()
    mock_resp_invalid.text = "}invalid yaml: {\n"
    mock_session.get.return_value = mock_resp_invalid

    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )
    assert "yaml_syntax_error" in str(coordinator.data[path]["last_error"])

    mock_resp_missing_bp = MagicMock(spec=httpx.Response)
    mock_resp_missing_bp.status_code = 200
    mock_resp_missing_bp.headers = {"Content-Type": "text/yaml"}
    mock_resp_missing_bp.raise_for_status = MagicMock()
    mock_resp_missing_bp.text = "other_key: value\nsource_url: https://url"
    mock_session.get.return_value = mock_resp_missing_bp

    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )
    assert "invalid_blueprint" in str(coordinator.data[path]["last_error"])

    mock_session.get.side_effect = httpx.ConnectError("Connection Failed")
    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )
    assert "fetch_error" in str(coordinator.data[path]["last_error"])
    assert "Connection Failed" in str(coordinator.data[path]["last_error"])


@pytest.mark.asyncio
async def test_async_update_blueprint_in_place_unsafe_url(coordinator):
    """Test that updating from an unsafe URL is blocked."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    path = "/config/blueprints/automation/test.yaml"
    info = {"source_url": "http://192.168.1.1/exploit", "domain": "automation"}

    coordinator.data = {path: info}
    with patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger:
        await coordinator._async_update_blueprint_in_place(MagicMock(), path, info, [], set())
        mock_logger.warning.assert_called_with("Blocking update from untrusted URL: (redacted URL)")


@pytest.mark.asyncio
async def test_async_update_blueprint_not_modified(coordinator):
    """Test the update flow when server returns 304 Not Modified."""
    path = "/config/blueprints/automation/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "source_url": "https://url",
        "domain": "automation",
        "local_hash": "old_hash",
    }
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "source_url": "https://url",
            "local_hash": "old_hash",
            "updatable": False,
            "remote_hash": "old_hash",
            "etag": "old_etag",
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 304
    mock_response.headers = {"ETag": "old_etag", "Content-Type": "text/yaml"}
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    results_to_notify = []
    updated_domains = set()
    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )

    assert coordinator.data[path]["etag"] == "old_etag"
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path].get("remote_content") is None


@pytest.mark.asyncio
async def test_async_update_data_uses_current_options(coordinator):
    """Test that _async_update_data uses the latest options from config_entry."""
    coordinator.config_entry.options = {
        "filter_mode": "whitelist",
        "selected_blueprints": ["test.yaml"],
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value={}) as mock_scan,
        patch.object(coordinator, "_start_background_refresh"),
        patch.object(coordinator, "_async_save_metadata"),
    ):
        await coordinator._async_update_data()
        mock_scan.assert_called_once_with(ANY, "whitelist", ["test.yaml"])


@pytest.mark.asyncio
async def test_async_handle_notifications_multiple_domains(coordinator):
    """Test that multiple domains are reloaded during auto-update notification."""
    coordinator.hass.services.has_service = MagicMock(return_value=True)
    coordinator.hass.services.async_call = AsyncMock()

    with (
        patch.object(coordinator, "async_translate", side_effect=lambda x, **_kw: x),
    ):
        await coordinator._async_handle_notifications(
            ["BP1", "BP2"], domains={"automation", "script"}
        )

    assert coordinator.hass.services.async_call.call_count >= 2
    coordinator.hass.services.async_call.assert_any_call("automation", "reload")
    coordinator.hass.services.async_call.assert_any_call("script", "reload")
