"""Tests for specific error handling and edge cases in BlueprintUpdateCoordinator."""

import asyncio
import contextlib
import os
from datetime import timedelta
from http import HTTPStatus
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import (
    DOMAIN,
    DOMAIN_AUTOMATION,
    ERROR_SEPARATOR,
    MAX_CONCURRENT_REQUESTS,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator

from .utils import mock_bounded_response


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

    coord._async_get_bounded_response = AsyncMock(side_effect=mock_bounded_response)
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
    coordinator._persisted_metadata = {
        "path1": {"etag": "etag1", "remote_hash": "hash1", "source_url": "u"},
        "path2": {"etag": "etag2", "remote_hash": "hash2", "source_url": "u"},
    }
    coordinator.data = {"path1": {"relative_path": "path1", "source_url": "u"}}

    with (
        patch("os.path.isfile", side_effect=lambda x: str(x).replace("\\", "/").endswith("path1")),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock) as mock_save,
        patch(
            "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
            side_effect=lambda hass, path: os.path.basename(path) if os.path.isfile(path) else None,
        ),
        patch.object(
            coordinator.hass,
            "async_create_background_task",
            side_effect=lambda coro, name=None: asyncio.create_task(coro),
        ) as mock_bg,
    ):
        await coordinator._async_prune_stale_metadata({"path1"})
        for call in mock_bg.call_args_list:
            if call.kwargs.get("name") == f"{DOMAIN}_prune_save":
                await call.args[0]
        mock_save.assert_awaited_once_with(force=True)

    assert "path2" not in coordinator._persisted_metadata
    assert "path1" in coordinator._persisted_metadata
    assert coordinator._persisted_metadata["path1"]["etag"] == "etag1"
    assert "path1" in coordinator.data


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
    with patch.object(coordinator, "_start_background_refresh") as mock_start:
        await coordinator._async_background_refresh(
            {},
            generation=coordinator._refresh_generation + 1,
        )
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
            "relative_path": f"automation/bp{i}.yaml",
            "source_url": f"https://url/bp{i}",
            "domain": DOMAIN_AUTOMATION,
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
        mock_response.status_code = HTTPStatus.OK
        mock_response.url = httpx.URL("https://mock_url")
        mock_response.text = "blueprint: name"
        mock_response.headers = {"Content-Type": "text/yaml"}
        mock_response.raise_for_status = MagicMock()
        return mock_response

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=slow_get)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_guarded_async_client",
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
        "relative_path": "automation/test.yaml",
        "source_url": source_url,
        "domain": DOMAIN_AUTOMATION,
        "local_hash": "old_hash",
    }
    coordinator.data = {path: info}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
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

    assert coordinator.data[path]["last_error"].startswith(f"fetch_error{ERROR_SEPARATOR}")
    assert coordinator.data[path]["updatable"] is False


@pytest.mark.asyncio
async def test_background_refresh_replaces_obsolete_generation(hass, coordinator):
    """A newer local scan replaces, rather than drops, obsolete remote work."""
    blueprints = {
        "path/1": {
            "name": "BP1",
            "relative_path": "path/1",
            "domain": DOMAIN_AUTOMATION,
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

        assert task1 is not task2
        assert task1.cancelled() or task1.cancelling()
        assert not task2.done()
        assert not task2.cancelled()

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
    assert entry["last_error"] == f"parse_error{ERROR_SEPARATOR}Invalid YAML found"


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
    assert entry["last_error"].startswith(f"download_error{ERROR_SEPARATOR}")
    assert "Failed to fetch content" in entry["last_error"]
    assert "\n" not in entry["last_error"]


def test_stabilize_yaml_structure_preserves_non_string_keys():
    """Test that _stabilize_yaml_structure preserves non-string key types and order."""
    orig = {1: "int_val", "1": "str_val", 2.5: "float_val"}
    norm = {1: "int_val_norm", "1": "str_val_norm", 2.5: "float_val_norm", 3: "new_int"}

    res = BlueprintUpdateCoordinator._stabilize_yaml_structure(orig, norm)

    assert isinstance(res, dict)
    res_dict: dict[object, object] = dict(res.items())
    # Check that key identity/types are preserved
    assert list(res_dict.keys()) == [1, "1", 2.5, 3]
    assert [type(key) for key in res_dict] == [int, str, float, int]
    assert res_dict[1] == "int_val_norm"
    assert res_dict["1"] == "str_val_norm"
    assert res_dict[2.5] == "float_val_norm"
    assert res_dict[3] == "new_int"
