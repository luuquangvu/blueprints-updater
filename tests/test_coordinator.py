import asyncio
import contextlib
import os
from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.blueprints_updater.const import (
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
    REQUEST_TIMEOUT,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(
            hass,
            entry,
            timedelta(hours=24),
        )

        coord._listeners = cast(Any, {})
        coord.hass = hass
        coord.data = {}

        def _mock_set_data(data):
            coord.data = data

        coord.async_set_updated_data = cast(Any, MagicMock(side_effect=_mock_set_data))
        coord.async_update_listeners = cast(Any, MagicMock())
        coord.setup_complete = True
        return coord


def test_normalize_url(coordinator):
    """Test URL normalization."""
    assert (
        coordinator._normalize_url("https://github.com/user/repo/blob/main/blueprints/test.yaml")
        == "https://raw.githubusercontent.com/user/repo/main/blueprints/test.yaml"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id/raw")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://community.home-assistant.io/t/topic-slug/12345")
        == "https://community.home-assistant.io/t/12345.json"
    )

    assert (
        coordinator._normalize_url("https://example.com/blueprint.yaml")
        == "https://example.com/blueprint.yaml"
    )


def test_parse_forum_content(coordinator):
    """Test parsing forum content."""
    json_data = {
        "post_stream": {
            "posts": [
                {
                    "cooked": (
                        '<p>Here is my blueprint:</p><pre><code class="lang-yaml">blueprint:\n'
                        "  name: Test\n  source_url: https://url.com</code></pre>"
                    )
                }
            ]
        }
    }
    content: Any = coordinator._parse_forum_content(json_data)
    assert "blueprint:" in content
    assert "name: Test" in content

    json_data_no_bp = {"post_stream": {"posts": [{"cooked": "<code>not a blueprint</code>"}]}}
    assert coordinator._parse_forum_content(json_data_no_bp) is None

    assert coordinator._parse_forum_content({}) is None
    assert coordinator._parse_forum_content({"post_stream": {"posts": []}}) is None


def test_ensure_source_url(coordinator):
    """Test ensuring source_url is present."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    content = "blueprint:\n  name: Test"
    new_content = coordinator._ensure_source_url(content, source_url)
    assert f"source_url: {source_url}" in new_content

    content_with_url = f"blueprint:\n  name: Test\n  source_url: {source_url}"
    assert coordinator._ensure_source_url(content_with_url, source_url) == content_with_url

    content_with_quotes = f"blueprint:\n  name: Test\n  source_url: '{source_url}'"
    assert coordinator._ensure_source_url(content_with_quotes, source_url) == content_with_quotes


def test_scan_blueprints(hass, coordinator):
    """Test scanning blueprints directory."""
    bp_path = "/config/blueprints"
    mock_files = [(bp_path, [], ["valid.yaml", "invalid.yaml", "no_url.yaml", "not_yaml.txt"])]

    valid_content = "blueprint:\n  name: Valid\n  source_url: https://url.com"
    invalid_content = "not: a blueprint"
    no_url_content = "blueprint:\n  name: No URL"

    def open_side_effect(path, *_args, **_kwargs):
        path_str = str(path)
        basename = os.path.basename(path_str)
        content = ""
        if basename == "valid.yaml":
            content = valid_content
        elif basename == "invalid.yaml":
            content = invalid_content
        elif basename == "no_url.yaml":
            content = no_url_content

        m = MagicMock()
        m.read.return_value = content
        m.__enter__.return_value = m
        return m

    with (
        patch("os.path.isdir", return_value=True),
        patch("os.walk", return_value=mock_files),
        patch("builtins.open", side_effect=open_side_effect),
    ):
        results = coordinator.scan_blueprints(hass, FILTER_MODE_ALL, [])
        assert len(results) == 1, f"Expected 1, got {len(results)}: {results.keys()}"
        assert any("valid.yaml" in k for k in results)
        full_path = next(iter(results.keys()))
        assert results[full_path]["rel_path"] == "valid.yaml"

        results = coordinator.scan_blueprints(hass, FILTER_MODE_WHITELIST, ["valid.yaml"])
        assert len(results) == 1

        results = coordinator.scan_blueprints(hass, FILTER_MODE_WHITELIST, ["other.yaml"])
        assert len(results) == 0

        results = coordinator.scan_blueprints(hass, FILTER_MODE_BLACKLIST, ["valid.yaml"])
        assert len(results) == 0


@pytest.mark.asyncio
async def test_async_update_blueprint(coordinator):
    """Test the full update flow for a single blueprint."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "hash": "old_hash",
    }
    results: dict[str, Any] = {path: {"last_error": None, "hash": "old_hash"}}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_etag"}
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
        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify
        )

    assert path in results
    assert results[path]["updatable"] is True
    assert results[path]["remote_hash"] == "new_hash"
    assert results[path]["etag"] == "new_etag"
    assert "source_url" in results[path]["remote_content"]

    kwargs = mock_session.get.call_args.kwargs
    assert kwargs["timeout"] == REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_async_update_blueprint_not_modified(coordinator):
    """Test the update flow when server returns 304 Not Modified."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://url",
        "hash": "old_hash",
    }
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": "https://url",
            "local_hash": "old_hash",
            "updatable": False,
            "remote_hash": "old_hash",
            "etag": "old_etag",
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 304
    mock_response.headers = {"ETag": "old_etag"}
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    results_to_notify = []
    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)

    assert coordinator.data[path]["etag"] == "old_etag"
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path].get("remote_content") is None


@pytest.mark.asyncio
async def test_async_install_blueprint(hass, coordinator):
    """Test installing a blueprint and reloading services."""
    path = "/config/blueprints/test.yaml"
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

    assert hass.services.async_call.call_count == 2
    hass.services.async_call.assert_any_call("automation", "reload")
    hass.services.async_call.assert_any_call("script", "reload")

    with pytest.raises(AssertionError):
        hass.services.async_call.assert_any_call("template", "reload")


@pytest.mark.asyncio
async def test_async_update_data_partial_failure(coordinator):
    """Test that one failed blueprint does not stop others."""
    blueprints = {
        "/config/blueprints/good.yaml": {
            "name": "Good",
            "rel_path": "good.yaml",
            "source_url": "https://url.com/good.yaml",
            "hash": "good_hash",
        },
        "/config/blueprints/bad.yaml": {
            "name": "Bad",
            "rel_path": "bad.yaml",
            "source_url": "https://url.com/bad.yaml",
            "hash": "bad_hash",
        },
    }

    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    mock_good_resp = MagicMock(spec=httpx.Response)
    mock_good_resp.status_code = 200
    mock_good_resp.headers = {"ETag": "good_etag"}
    mock_good_resp.raise_for_status = MagicMock()
    mock_good_resp.text = "blueprint:\n  name: Good"

    mock_bad_resp = MagicMock(spec=httpx.Response)
    mock_bad_resp.status_code = 404
    mock_bad_resp.headers = {}
    mock_bad_resp.raise_for_status = MagicMock(side_effect=Exception("404 Not Found"))

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client"
        ) as mock_session_class,
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_session = MagicMock(spec=httpx.AsyncClient)
        mock_session_class.return_value = mock_session

        mock_session.get = AsyncMock(
            side_effect=lambda url, **_kwargs: (
                mock_good_resp if "good.yaml" in url else mock_bad_resp
            )
        )
        mock_hash.return_value.hexdigest.return_value = "new_hash"

        with patch.object(coordinator, "_start_background_refresh"):
            update_results = await coordinator._async_update_data()
            coordinator.data = update_results
            await coordinator._async_background_refresh(blueprints)
            results = update_results

    assert "/config/blueprints/bad.yaml" in results

    assert results["/config/blueprints/good.yaml"]["updatable"] is True
    assert results["/config/blueprints/good.yaml"]["last_error"] is None

    assert results["/config/blueprints/bad.yaml"]["last_error"] is not None
    assert "404" in results["/config/blueprints/bad.yaml"]["last_error"]


@pytest.mark.asyncio
async def test_async_background_refresh_503_resilience(coordinator):
    """Test that 503 errors do NOT abort the refresh cycle anymore."""
    blueprints = {
        "/config/blueprints/b1.yaml": {
            "name": "B1",
            "rel_path": "b1.yaml",
            "source_url": "https://url/b1",
            "hash": "h1",
        },
        "/config/blueprints/b2.yaml": {
            "name": "B2",
            "rel_path": "b2.yaml",
            "source_url": "https://url/b2",
            "hash": "h2",
        },
    }

    mock_503_resp = MagicMock(spec=httpx.Response)
    mock_503_resp.status_code = 503
    mock_503_resp.headers = {}
    mock_503_resp.raise_for_status = MagicMock(
        side_effect=Exception("503 Backend.max_conn reached")
    )

    mock_200_resp = MagicMock(spec=httpx.Response)
    mock_200_resp.status_code = 200
    mock_200_resp.text = "blueprint:\n  name: B2"
    mock_200_resp.headers = {"ETag": "e2"}
    mock_200_resp.raise_for_status = MagicMock()

    coordinator.data = {
        "/config/blueprints/b1.yaml": {"last_error": None},
        "/config/blueprints/b2.yaml": {"last_error": None},
    }

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client"
        ) as mock_session_class,
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_session = MagicMock(spec=httpx.AsyncClient)
        mock_session_class.return_value = mock_session

        mock_session.get = AsyncMock(
            side_effect=lambda url, **_kwargs: mock_503_resp if "b1" in url else mock_200_resp
        )
        mock_hash.return_value.hexdigest.return_value = "new_hash"
        await coordinator._async_background_refresh(blueprints)

    assert "503" in str(coordinator.data["/config/blueprints/b1.yaml"]["last_error"])
    assert coordinator.data["/config/blueprints/b2.yaml"]["updatable"] is True
    assert coordinator.data["/config/blueprints/b2.yaml"]["last_error"] is None


@pytest.mark.asyncio
async def test_async_background_refresh_semaphore_limit(coordinator):
    """Test that background refresh respects MAX_CONCURRENT_REQUESTS."""
    num_blueprints = MAX_CONCURRENT_REQUESTS + 2
    blueprints = {
        f"/bp{i}.yaml": {
            "name": f"BP{i}",
            "rel_path": f"bp{i}.yaml",
            "source_url": f"https://url/bp{i}",
            "hash": "h",
        }
        for i in range(num_blueprints)
    }

    active_requests = 0
    max_active_requests = 0
    lock = asyncio.Lock()
    barrier = asyncio.Barrier(MAX_CONCURRENT_REQUESTS)

    async def slow_get(*_args, **_kwargs):
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
        mock_response.headers = {}
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
    ):
        await coordinator._async_background_refresh(blueprints)

    assert max_active_requests == MAX_CONCURRENT_REQUESTS


@pytest.mark.asyncio
async def test_async_update_blueprint_in_place_errors(coordinator):
    """Test various error conditions in _async_update_blueprint_in_place."""
    path = "/config/blueprints/test.yaml"
    info = {"name": "Test", "source_url": "https://url", "hash": "hash"}
    results = {
        path: {
            "last_error": None,
            "hash": "hash",
            "name": "Test",
            "source_url": "https://url",
        }
    }
    coordinator.data = results

    mock_resp_empty = MagicMock(spec=httpx.Response)
    mock_resp_empty.status_code = 200
    mock_resp_empty.headers = {}
    mock_resp_empty.raise_for_status = MagicMock()
    mock_resp_empty.text = ""

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_resp_empty)
    results_to_notify = []

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert coordinator.data[path]["last_error"] == "empty_content"

    mock_resp_invalid = MagicMock(spec=httpx.Response)
    mock_resp_invalid.status_code = 200
    mock_resp_invalid.headers = {}
    mock_resp_invalid.raise_for_status = MagicMock()
    mock_resp_invalid.text = "}invalid yaml: {\n"
    mock_session.get.return_value = mock_resp_invalid

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "yaml_syntax_error" in str(coordinator.data[path]["last_error"])

    mock_resp_missing_bp = MagicMock(spec=httpx.Response)
    mock_resp_missing_bp.status_code = 200
    mock_resp_missing_bp.headers = {}
    mock_resp_missing_bp.raise_for_status = MagicMock()
    mock_resp_missing_bp.text = "other_key: value\nsource_url: https://url"
    mock_session.get.return_value = mock_resp_missing_bp

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "invalid_blueprint" in str(coordinator.data[path]["last_error"])

    mock_session.get.side_effect = Exception("Connection Failed")
    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "fetch_error" in str(coordinator.data[path]["last_error"])
    assert "Connection Failed" in str(coordinator.data[path]["last_error"])


@pytest.mark.asyncio
async def test_async_install_blueprint_error(coordinator):
    """Test exception during blueprint installation."""
    with (
        patch("builtins.open", side_effect=Exception("Write failed")),
        pytest.raises(Exception, match="Write failed"),
    ):
        await coordinator.async_install_blueprint("/fake/path.yaml", "content")


@pytest.mark.asyncio
async def test_async_update_data_auto_update(coordinator):
    """Test _async_update_data with auto_update enabled."""
    coordinator.config_entry.options = MappingProxyType({"auto_update": True})
    blueprints = {
        "/test.yaml": {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": "https://url",
            "hash": "old",
        }
    }
    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            new_callable=MagicMock,
        ) as mock_session_class,
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "async_install_blueprint") as mock_install,
        patch.object(coordinator, "async_reload_services") as mock_reload,
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_translations",
            return_value={
                "component.blueprints_updater.common.auto_update_title": "Title",
                "component.blueprints_updater.common.auto_update_message": "Msg {blueprints}",
            },
        ),
    ):
        mock_session = MagicMock(spec=httpx.AsyncClient)
        mock_session_class.return_value = mock_session

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {"ETag": "new"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "blueprint:\n  name: Test\n  source_url: https://url"
        mock_session.get = AsyncMock(return_value=mock_resp)

        mock_hash.return_value.hexdigest.return_value = "new"

        with patch.object(coordinator, "_start_background_refresh"):
            results = await coordinator._async_update_data()
            coordinator.data = results
            await coordinator._async_background_refresh(blueprints)

        mock_install.assert_called_once_with(
            "/test.yaml",
            "blueprint:\n  name: Test\n  source_url: https://url",
            reload_services=False,
            backup=True,
        )
        mock_reload.assert_called_once()

        coordinator.hass.services.async_call.assert_any_call(
            "persistent_notification",
            "create",
            {
                "title": "Title",
                "message": "Msg - Test",
                "notification_id": "blueprints_updater_auto_update",
            },
        )

        assert "/test.yaml" in coordinator.data
        assert coordinator.data["/test.yaml"]["updatable"] is False
        assert coordinator.data["/test.yaml"]["remote_content"] is None
        assert coordinator.data["/test.yaml"]["local_hash"] == "new"
        assert coordinator.data["/test.yaml"]["etag"] == "new"


@pytest.mark.asyncio
async def test_async_update_data_auto_update_multiple_sorted(coordinator):
    """Test _async_update_data sorts multiple auto-updated blueprints."""
    coordinator.config_entry.options = MappingProxyType({"auto_update": True})
    blueprints = {
        "/b.yaml": {
            "name": "Beta",
            "rel_path": "b.yaml",
            "source_url": "https://url/b",
            "hash": "old",
        },
        "/a.yaml": {
            "name": "Alpha",
            "rel_path": "a.yaml",
            "source_url": "https://url/a",
            "hash": "old",
        },
    }
    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            new_callable=MagicMock,
        ) as mock_session_class,
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "async_install_blueprint"),
        patch.object(coordinator, "async_reload_services"),
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_translations",
            return_value={
                "component.blueprints_updater.common.auto_update_title": "Title",
                "component.blueprints_updater.common.auto_update_message": "Msg\n{blueprints}",
            },
        ),
    ):
        mock_session = MagicMock(spec=httpx.AsyncClient)
        mock_session_class.return_value = mock_session

        mock_resp_a = MagicMock(spec=httpx.Response)
        mock_resp_a.status_code = 200
        mock_resp_a.headers = {"ETag": "new"}
        mock_resp_a.raise_for_status = MagicMock()
        mock_resp_a.text = "blueprint:\n  name: Alpha\n  source_url: https://url/a"

        mock_resp_b = MagicMock(spec=httpx.Response)
        mock_resp_b.status_code = 200
        mock_resp_b.headers = {"ETag": "new"}
        mock_resp_b.raise_for_status = MagicMock()
        mock_resp_b.text = "blueprint:\n  name: Beta\n  source_url: https://url/b"

        mock_session.get = AsyncMock(
            side_effect=lambda url, **_kwargs: mock_resp_a if "/a" in url else mock_resp_b
        )
        mock_hash.return_value.hexdigest.return_value = "new"

        with patch.object(coordinator, "_start_background_refresh"):
            results = await coordinator._async_update_data()
            coordinator.data = results
            await coordinator._async_background_refresh(blueprints)

        args = coordinator.hass.services.async_call.call_args_list
        notification_call = next(
            c for c in args if c.args[0] == "persistent_notification" and c.args[1] == "create"
        )
        message = notification_call.args[2]["message"]

        assert "Alpha" in message
        assert "Beta" in message
        assert message.index("Alpha") < message.index("Beta")
        assert message == "Msg\n- Alpha\n- Beta"


@pytest.mark.asyncio
async def test_async_install_blueprint_backup(hass, coordinator):
    """Test installing a blueprint with backup enabled."""
    path = "/config/blueprints/test.yaml"
    remote_content = "blueprint:\n  name: Test"

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 3})
    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2") as mock_copy,
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
    ):
        await coordinator.async_install_blueprint(path, remote_content, backup=True)

    mock_copy.assert_called_once_with(path, f"{path}.bak.1")
    mock_replace.assert_any_call(f"{path}.tmp", path)


@pytest.mark.asyncio
async def test_async_restore_blueprint_success(hass, coordinator):
    """Test successful restoration of a blueprint backup."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
    ):
        result = await coordinator.async_restore_blueprint(path)

    mock_replace.assert_called_once_with(f"{path}.bak.1", path)
    assert result["success"] is True
    assert result["translation_key"] == "success"
    hass.services.async_call.assert_any_call("automation", "reload")


@pytest.mark.asyncio
async def test_async_restore_blueprint_missing(hass, coordinator):
    """Test restoration when backup is missing."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.exists", return_value=False
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "missing_backup"


@pytest.mark.asyncio
async def test_async_restore_blueprint_error(hass, coordinator):
    """Test error handling during blueprint restoration."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.os.replace",
            side_effect=Exception("Disk error"),
        ),
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "system_error"
    assert "Disk error" in result["translation_kwargs"]["error"]


def test_validate_blueprint_valid(coordinator):
    """Test _validate_blueprint with valid data returns None."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is None


def test_validate_blueprint_incompatible_version(coordinator):
    """Test _validate_blueprint blocks when min_version is too high."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
            "homeassistant": {"min_version": "2099.1.0"},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "incompatible" in result
    assert "2099.1.0" in result


def test_validate_blueprint_schema_error(coordinator):
    """Test _validate_blueprint catches schema validation errors."""
    data = {"blueprint": {"name": "Test"}}
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "validation_error" in result


def test_validate_blueprint_missing_key(coordinator):
    """Test _validate_blueprint with data missing the 'blueprint' key."""
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint({"not_blueprint": {}}, "https://example.com/bp.yaml")
    assert result is not None
    assert "invalid_blueprint" in result


@pytest.mark.asyncio
async def test_backup_rotation(coordinator, tmp_path):
    """Test that backups rotate correctly: .bak.1 is newest, .bak.3 is oldest."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("version_0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 3})
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    for i in range(1, 4):
        await coordinator.async_install_blueprint(
            str(bp_file), f"version_{i}", reload_services=False, backup=True
        )

    assert bp_file.read_text() == "version_3"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "version_2"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "version_1"
    assert (tmp_path / "test.yaml.bak.3").read_text() == "version_0"


@pytest.mark.asyncio
async def test_backup_max_limit(coordinator, tmp_path):
    """Test that backups exceeding max_backups are cleaned up."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("v0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 2})
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    for i in range(1, 5):
        await coordinator.async_install_blueprint(
            str(bp_file), f"v{i}", reload_services=False, backup=True
        )

    assert bp_file.read_text() == "v4"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "v3"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "v2"
    assert not (tmp_path / "test.yaml.bak.3").exists()


@pytest.mark.asyncio
async def test_restore_versioned(coordinator, tmp_path):
    """Test restoring from a specific backup version."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("current")
    (tmp_path / "test.yaml.bak.1").write_text("backup_v1")
    (tmp_path / "test.yaml.bak.2").write_text("backup_v2")

    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    coordinator.async_reload_services = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    result = await coordinator.async_restore_blueprint(str(bp_file), version=2)
    assert result["success"] is True
    assert bp_file.read_text() == "backup_v2"
    assert not (tmp_path / "test.yaml.bak.2").exists()


@pytest.mark.asyncio
async def test_backup_migration_old_bak(coordinator, tmp_path):
    """Test migration of old .bak format to .bak.1 on next backup."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("current")
    (tmp_path / "test.yaml.bak").write_text("old_backup")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 3})
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    await coordinator.async_install_blueprint(
        str(bp_file), "new_version", reload_services=False, backup=True
    )

    assert bp_file.read_text() == "new_version"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "current"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "old_backup"
    assert not (tmp_path / "test.yaml.bak").exists()


async def test_background_refresh_deduplication(hass, coordinator):
    """Test that multiple refresh requests do not start duplicate background tasks."""
    blueprints = {
        "path/1": {"name": "BP1", "rel_path": "path/1", "source_url": "url1", "hash": "h1"}
    }
    coordinator.config_entry.options = MappingProxyType(
        {
            "filter_mode": "all",
            "selected_blueprints": [],
        }
    )

    async def mock_refresh(*_args, **_kwargs):
        await asyncio.sleep(10)

    def side_effect(coro, name=None):
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


async def test_background_refresh_shutdown(hass, coordinator):
    """Test that shutdown cancels the background task."""

    async def long_running_task():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise

    def side_effect(coro, name=None):
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


@pytest.mark.asyncio
async def test_async_fetch_content_retry_limit(coordinator):
    """Test that _async_fetch_content retries exactly MAX_RETRIES times."""
    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=Exception("Fetch failed"))

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        pytest.raises(Exception, match="Fetch failed"),
    ):
        await coordinator._async_fetch_content(mock_session, "https://url")

    assert mock_session.get.call_count == MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_async_fetch_content_pacing_logic(coordinator):
    """Test that _async_fetch_content respects MIN_SEND_INTERVAL pacing."""
    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = "content"
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()

    mock_session.get = AsyncMock(return_value=mock_response)

    time_points = [100.0, 100.1]
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.time.monotonic",
            side_effect=time_points,
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.random.uniform",
            return_value=MIN_SEND_INTERVAL,
        ) as mock_random,
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep,
    ):
        await coordinator._async_fetch_content(mock_session, "https://url1")
        await coordinator._async_fetch_content(mock_session, "https://url2")

        mock_random.assert_called_with(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)

        expected_delay = (100.0 + MIN_SEND_INTERVAL) - 100.1
        mock_sleep.assert_called_with(expected_delay)


@pytest.mark.asyncio
async def test_async_fetch_content_pacing_logic_max(coordinator):
    """Test that _async_fetch_content respects MAX_SEND_INTERVAL pacing."""
    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = "content"
    mock_response.headers = {}
    mock_response.raise_for_status = MagicMock()

    mock_session.get = AsyncMock(return_value=mock_response)
    coordinator._last_request_time = 0.0

    time_points = [200.0, 200.1]
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.time.monotonic",
            side_effect=time_points,
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.random.uniform",
            return_value=MAX_SEND_INTERVAL,
        ) as mock_random,
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep,
    ):
        await coordinator._async_fetch_content(mock_session, "https://url1")
        await coordinator._async_fetch_content(mock_session, "https://url2")

        mock_random.assert_called_with(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)

        expected_delay = (200.0 + MAX_SEND_INTERVAL) - 200.1
        mock_sleep.assert_called_with(expected_delay)
