"""Tests for specific error handling and edge cases in BlueprintUpdateCoordinator."""

import asyncio
import contextlib
from datetime import timedelta
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import (
    DOMAIN,
    MAX_CONCURRENT_REQUESTS,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator used in edge case tests."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    coord.hass = hass
    coord.config_entry = entry
    coord.setup_complete = True
    return coord


@pytest.mark.asyncio
async def test_async_translate_error_handling(coordinator):
    """Test error handling in async_translate."""
    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        side_effect=OSError("Disk full"),
    ):
        result = await coordinator.async_translate("test_key")
        assert result == "test_key"


@pytest.mark.asyncio
async def test_extract_inputs_schema_malformed(coordinator):
    """Test _extract_inputs_schema with malformed YAML."""
    schema, error = coordinator._extract_inputs_schema("not a yaml dict")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("automation: test")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("blueprint: { input: [] }")
    assert schema == {}
    assert error is None

    schema, error = coordinator._extract_inputs_schema("blueprint: { input: { test: true } }")
    assert schema["test"]["mandatory"] is True
    assert error is None


def test_extract_inputs_schema_exception(coordinator):
    """Test _extract_inputs_schema with forced exception."""
    with patch("homeassistant.util.yaml.parse_yaml", side_effect=HomeAssistantError("forced")):
        schema, error = coordinator._extract_inputs_schema("any")
        assert schema == {}
        assert error == "forced"


@pytest.mark.asyncio
async def test_merge_previous_data_edge_cases(coordinator):
    """Test merge_previous_data with malformed previous data."""
    results = {"path1": {"local_hash": "A", "remote_hash": "B", "updatable": True}}

    coordinator.data = {}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is True
    results = {"path1": {"local_hash": "A", "remote_hash": "A", "updatable": False}}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is False

    coordinator.data = {"path1": "not a dict"}
    results = {"path1": {"local_hash": "A", "remote_hash": "B", "updatable": True}}
    coordinator._merge_previous_data(results)
    assert results["path1"]["updatable"] is True


@pytest.mark.asyncio
async def test_prune_stale_metadata(coordinator):
    """Test pruning logic for stale metadata."""
    coordinator._persisted_etags = {"path1": "etag1", "path2": "etag2"}
    coordinator._persisted_hashes = {"path1": "hash1", "path2": "hash2"}
    coordinator.data = {"path1": {}}

    with (
        patch("os.path.isfile", side_effect=lambda x: x == "path1"),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock) as mock_save,
        patch.object(
            coordinator.hass,
            "async_create_background_task",
            side_effect=lambda coro, name=None: coro,
        ) as mock_bg,
    ):
        await coordinator._async_prune_stale_metadata({"path1"})
        for call in mock_bg.call_args_list:
            if call.kwargs.get("name") == f"{DOMAIN}_prune_save":
                await call.args[0]
        mock_save.assert_awaited_once_with(force=True)

    assert "path2" not in coordinator._persisted_etags
    assert "path2" not in coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_save_metadata_safety(coordinator):
    """Test safety check in save_metadata."""
    coordinator.setup_complete = False
    with patch.object(coordinator._store, "async_save") as mock_save:
        await coordinator._async_save_metadata()
        mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_coordinator_misc(coordinator: BlueprintUpdateCoordinator):
    """Test misc coordinator paths."""
    coordinator._refresh_lock = MagicMock()
    coordinator._refresh_lock.locked.return_value = True
    with patch.object(coordinator, "_start_background_refresh") as mock_start:
        await coordinator._async_background_refresh({})
        assert coordinator._background_task is None
        mock_start.assert_not_called()

    mock_task = MagicMock()
    mock_task.done.return_value = False
    coordinator._background_task = mock_task
    coordinator._async_cancel_background_task()
    mock_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_async_background_refresh_semaphore_limit(coordinator):
    """Test that background refresh respects MAX_CONCURRENT_REQUESTS."""
    num_blueprints = MAX_CONCURRENT_REQUESTS + 2
    blueprints = {
        f"automation/bp{i}.yaml": {
            "name": f"BP{i}",
            "rel_path": f"automation/bp{i}.yaml",
            "source_url": f"https://url/bp{i}",
            "domain": "automation",
            "local_hash": "h",
        }
        for i in range(num_blueprints)
    }

    active_requests = 0
    max_active_requests = 0
    lock = asyncio.Lock()
    barrier = asyncio.Barrier(MAX_CONCURRENT_REQUESTS)

    async def slow_get(*_args, **_kwargs):
        """Mock slow_get."""
        nonlocal active_requests, max_active_requests
        async with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(barrier.wait(), timeout=1.0)

        async with lock:
            active_requests -= 1

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = "blueprint: name"
        mock_response.headers = {"Content-Type": "text/yaml"}
        mock_response.raise_for_status = MagicMock()
        return mock_response

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=slow_get)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=mock_session,
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch.object(coordinator, "_is_safe_url", AsyncMock(return_value=True)),
    ):
        await coordinator._async_background_refresh(blueprints)

    assert max_active_requests == MAX_CONCURRENT_REQUESTS


@pytest.mark.asyncio
async def test_async_fetch_content_forum_invalid_json_sets_fetch_error(coordinator):
    """Test that invalid JSON from forum URLs sets fetch_error."""
    path = "/config/blueprints/automation/test.yaml"
    source_url = "https://community.home-assistant.io/t/123"
    info = {
        "name": "Test",
        "rel_path": "automation/test.yaml",
        "source_url": source_url,
        "domain": "automation",
        "local_hash": "old_hash",
    }
    coordinator.data = {path: info}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.url = httpx.URL(source_url)
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.text = '{"posts": [ {"cooked": "invalid"}'
    mock_response.json = MagicMock(side_effect=ValueError("Expecting value"))
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    results_to_notify = []
    updated_domains = set()

    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )

    assert coordinator.data[path]["last_error"].startswith("fetch_error|")
    assert coordinator.data[path]["updatable"] is False


@pytest.mark.asyncio
async def test_background_refresh_deduplication(hass, coordinator):
    """Test that multiple refresh requests do not start duplicate background tasks."""
    blueprints = {
        "path/1": {
            "name": "BP1",
            "rel_path": "path/1",
            "domain": "automation",
            "source_url": "url1",
            "local_hash": "h1",
        }
    }
    coordinator.config_entry.options = MappingProxyType(
        {
            "filter_mode": "all",
            "selected_blueprints": [],
        }
    )

    async def mock_refresh(*_args, **_kwargs):
        """Mock mock_refresh."""
        await asyncio.sleep(10)

    def side_effect(coro, name=None):
        """Mock side_effect."""
        return asyncio.create_task(coro, name=name)

    hass.async_create_background_task = MagicMock(side_effect=side_effect)
    with (
        patch.object(coordinator.__class__, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_async_background_refresh", side_effect=mock_refresh),
    ):
        await coordinator._async_update_data()
        task1: Any = coordinator._background_task
        assert task1 is not None

        await coordinator._async_update_data()
        task2 = coordinator._background_task

        assert task1 is task2
        assert not task1.done()
        assert not task1.cancelled()

        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_background_refresh_shutdown(hass, coordinator):
    """Test that shutdown cancels the background task."""

    async def long_running_task():
        """Mock long_running_task."""
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise

    def side_effect(coro, name=None):
        """Mock side_effect."""
        return asyncio.create_task(coro, name=name)

    hass.async_create_background_task = MagicMock(side_effect=side_effect)

    coordinator._background_task = hass.async_create_background_task(
        long_running_task(), name="test_shutdown"
    )

    task: Any = coordinator._background_task
    assert not task.done()

    await coordinator.async_shutdown()

    assert task.cancelled()
    assert coordinator._background_task is None


def test_update_error_state_clears_state_and_etag(coordinator):
    """Test that _update_error_state clears core state and ETag when clear_etag=True."""
    path = "script/test_blueprint.yaml"
    coordinator.data[path] = {
        "remote_hash": "old-hash",
        "remote_content": "old-content",
        "updatable": True,
        "etag": "etag-123",
        "invalid_remote_hash": "invalid-hash",
    }

    coordinator._update_error_state(
        path,
        error_type="parse_error",
        detail="Invalid YAML found",
        clear_etag=True,
    )

    entry = coordinator.data[path]
    assert entry["remote_hash"] is None
    assert entry["remote_content"] is None
    assert entry["updatable"] is False
    assert entry["etag"] is None
    assert entry["invalid_remote_hash"] is None
    assert entry["last_error"] == "parse_error|Invalid YAML found"


def test_update_error_state_clears_state_and_keeps_etag(coordinator):
    """Test that _update_error_state clears core state but preserves ETag when clear_etag=False."""
    path = "script/test_blueprint.yaml"
    coordinator.data[path] = {
        "remote_hash": "old-hash",
        "remote_content": "old-content",
        "updatable": True,
        "etag": "etag-123",
        "invalid_remote_hash": "invalid-hash",
    }

    coordinator._update_error_state(
        path,
        error_type="download_error",
        detail="Failed to fetch content\nNewlines should be sanitized",
        clear_etag=False,
    )

    entry = coordinator.data[path]
    assert entry["remote_hash"] is None
    assert entry["remote_content"] is None
    assert entry["updatable"] is False
    assert entry["invalid_remote_hash"] is None
    assert entry["etag"] == "etag-123"
    assert entry["last_error"].startswith("download_error|")
    assert "Failed to fetch content" in entry["last_error"]
    assert "\n" not in entry["last_error"]
