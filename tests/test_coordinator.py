"""Tests for Blueprints Updater coordinator."""

import asyncio
import contextlib
import hashlib
import os
import socket
from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, mock_open, patch

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util
from protocols import (
    BlueprintCoordinatorInternal,
    BlueprintCoordinatorProtocol,
    BlueprintCoordinatorPublic,
)

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
def coordinator(hass) -> BlueprintCoordinatorProtocol:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = cast(
            BlueprintCoordinatorProtocol,
            BlueprintUpdateCoordinator(
                hass,
                entry,
                timedelta(hours=24),
            ),
        )

        coord._listeners = {}
        coord.hass = hass
        coord.data = {}

        def _mock_set_data(data):
            """Mock _mock_set_data."""
            coord.data = data

        coord.async_set_updated_data = cast(Any, MagicMock(side_effect=_mock_set_data))
        coord.async_update_listeners = cast(Any, MagicMock())
        coord.setup_complete = True
        coord.last_update_success = True
        coord._is_safe_path = cast(Any, MagicMock(return_value=True))
        coord._is_safe_url = cast(Any, AsyncMock(return_value=True))
        return coord


def test_coordinator_protocol_conformance(coordinator):
    """Verify that BlueprintUpdateCoordinator conforms to BlueprintCoordinatorProtocol.

    This test ensures that the coordinator implementation adheres to the defined
    protocols for public, internal, and combined interfaces using runtime protocol
    checks.
    """
    assert isinstance(coordinator, BlueprintCoordinatorPublic)
    assert isinstance(coordinator, BlueprintCoordinatorInternal)
    assert isinstance(coordinator, BlueprintCoordinatorProtocol)


@pytest.mark.asyncio
async def test_async_install_blueprint_reload_fallback(coordinator):
    """Test that reload fallback works when blueprint block is missing or malformed."""
    path = "test.yaml"
    content = "invalid: yaml"

    coordinator.async_reload_services = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
    ):
        await coordinator.async_install_blueprint(path, content, reload_services=True)
    coordinator.async_reload_services.assert_called_once_with(["automation"])

    coordinator.async_reload_services.reset_mock()
    coordinator.data = {path: {"domain": "script", "name": "Test"}}
    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
    ):
        await coordinator.async_install_blueprint(path, content, reload_services=True)
    coordinator.async_reload_services.assert_called_once_with(["script"])


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

    new_content = coordinator._ensure_source_url("blueprint:\n  name: Test", source_url)
    assert f"source_url: {source_url}" in new_content

    content_with_url = f"blueprint:\n  name: Test\n  source_url: {source_url}"
    assert coordinator._ensure_source_url(content_with_url, source_url) == content_with_url

    content_with_quotes = f"blueprint:\n  name: Test\n  source_url: '{source_url}'"
    assert coordinator._ensure_source_url(content_with_quotes, source_url) == content_with_quotes

    different_url = "https://github.com/user/new-repo/blob/main/test.yaml"
    content_different = f"blueprint:\n  name: Test\n  source_url: {different_url}"
    result = coordinator._ensure_source_url(content_different, source_url)
    assert result == content_different
    assert result.count("source_url") == 1

    content_outside = (
        "blueprint:\n  name: Test\n  domain: automation\n"
        "action:\n  - service: rest.post\n    data:\n"
        "      source_url: https://api.example.com"
    )
    result_outside = coordinator._ensure_source_url(content_outside, source_url)
    assert f"source_url: {source_url}" in result_outside
    bp_block = result_outside.split("action:")[0]
    assert f"source_url: {source_url}" in bp_block

    content_nested_input = (
        "blueprint:\n  name: Test\n  domain: automation\n"
        "  input:\n    source_url:\n      name: Enter URL\n"
        "trigger:\n  - platform: webhook"
    )
    result_nested = coordinator._ensure_source_url(content_nested_input, source_url)
    assert f"  source_url: {source_url}" in result_nested
    assert result_nested.count("source_url") == 2

    content_with_comment = "blueprint: # comment\n  name: Test"
    result_comment = coordinator._ensure_source_url(content_with_comment, source_url)
    assert f"source_url: {source_url}" in result_comment
    assert "blueprint: # comment" in result_comment

    content_flow = "blueprint: { name: Test }"
    result_flow = coordinator._ensure_source_url(content_flow, source_url)
    assert f"source_url: {source_url}" in result_flow

    content_multi = (
        "# Some info: blueprint:\n"
        "blueprint:\n"
        "  name: Test\n"
        "description: This is another blueprint: key in string"
    )
    result_multi = coordinator._ensure_source_url(content_multi, source_url)
    assert result_multi.count(f"source_url: {source_url}") == 1
    assert "source_url:" in result_multi.split("description:")[0]

    content_none = "not_a_blueprint: true"
    assert coordinator._ensure_source_url(content_none, source_url) == content_none


def test_ensure_source_url_indented_key(coordinator):
    """Test that indented blueprint keys do NOT trigger injection."""
    source_url = "https://url.com/blueprint.yaml"
    content = """
not_blueprint:
  something: else
  blueprint:
    nested: true
"""
    assert coordinator._ensure_source_url(content, source_url) == content


@pytest.mark.asyncio
async def test_async_fetch_content_forum_invalid_json_sets_fetch_error(coordinator):
    """Test that invalid JSON from forum URLs sets fetch_error."""
    path = "/config/blueprints/test.yaml"
    source_url = "https://community.home-assistant.io/t/123"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": source_url,
        "domain": "automation",
        "local_hash": "old_hash",
    }
    coordinator.data = {path: info}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
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

    assert "fetch_error" in coordinator.data[path]["last_error"]
    assert "Invalid JSON response" in coordinator.data[path]["last_error"]
    assert "123.json" in coordinator.data[path]["last_error"]
    assert coordinator.data[path]["updatable"] is False


def test_scan_blueprints(hass, coordinator):
    """Test scanning blueprints directory."""
    bp_path = "/config/blueprints"
    mock_files = [(bp_path, [], ["valid.yaml", "invalid.yaml", "no_url.yaml", "not_yaml.txt"])]

    valid_content = "blueprint:\n  name: Valid\n  source_url: https://url.com"
    invalid_content = "not: a blueprint"
    no_url_content = "blueprint:\n  name: No URL"

    def open_side_effect(path, *_args, **_kwargs):
        """Mock open_side_effect."""
        path_str = str(path)
        basename = os.path.basename(path_str)
        contents_map = {
            "valid.yaml": valid_content,
            "invalid.yaml": invalid_content,
            "no_url.yaml": no_url_content,
        }
        content = contents_map.get(basename, "")

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
async def test_async_fetch_blueprint_force(coordinator):
    """Test that async_fetch_blueprint with force=True bypasses ETag."""
    path = "/config/blueprints/test.yaml"
    source_url = "https://url/test.yaml"
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": source_url,
            "local_hash": "old_hash",
            "updatable": True,
            "remote_hash": "new_hash",
            "etag": "stored_etag",
            "remote_content": None,
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_etag"}
    mock_response.text = "blueprint:\n  name: Test\n  source_url: https://url/test.yaml"
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=mock_session,
        ),
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_hash.return_value.hexdigest.return_value = "new_hash"
        await coordinator.async_fetch_blueprint(path, force=True)

    _args, kwargs = mock_session.get.call_args
    assert "If-None-Match" not in kwargs.get("headers", {})
    content = coordinator.data[path]["remote_content"]
    assert isinstance(content, str)
    assert "source_url" in content
    assert coordinator.data[path]["etag"] == "new_etag"


@pytest.mark.asyncio
async def test_async_update_blueprint_304_auto_update(coordinator):
    """Test that auto-update works even if the fetch returns 304."""
    path = "/config/blueprints/test.yaml"
    source_url = "https://url/test.yaml"

    coordinator.config_entry.options = MappingProxyType({"auto_update": True})

    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
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
    mock_response_200.headers = {"ETag": "stored_etag"}
    mock_response_200.text = "blueprint:\n  name: Test\n  source_url: https://url/test.yaml"
    mock_response_200.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=[mock_response_304, mock_response_200])

    with (
        patch.object(
            coordinator, "async_install_blueprint", new_callable=AsyncMock
        ) as mock_install,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
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
async def test_async_update_blueprint(coordinator):
    """Test the full update flow for a single blueprint."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "domain": "automation",
        "local_hash": "old_hash",
    }
    results: dict[str, Any] = {path: {"last_error": None, "local_hash": "old_hash"}}

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
async def test_async_update_blueprint_not_modified(coordinator):
    """Test the update flow when server returns 304 Not Modified."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://url",
        "domain": "automation",
        "local_hash": "old_hash",
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
    updated_domains = set()
    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )

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

    assert hass.services.async_call.call_count == 1
    hass.services.async_call.assert_any_call("automation", "reload")

    with pytest.raises(AssertionError):
        hass.services.async_call.assert_any_call("template", "reload")


@pytest.mark.asyncio
async def test_async_install_blueprint_domain_normalization(hass, coordinator):
    """Test that async_install_blueprint correctly normalizes the domain."""
    path = "/config/blueprints/test.yaml"

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
async def test_async_update_data_partial_failure(coordinator):
    """Test that one failed blueprint does not stop others."""
    blueprints = {
        "/config/blueprints/good.yaml": {
            "name": "Good",
            "rel_path": "good.yaml",
            "source_url": "https://url.com/good.yaml",
            "domain": "automation",
            "local_hash": "good_hash",
        },
        "/config/blueprints/bad.yaml": {
            "name": "Bad",
            "rel_path": "bad.yaml",
            "source_url": "https://url.com/bad.yaml",
            "domain": "automation",
            "local_hash": "bad_hash",
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
    mock_bad_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=mock_bad_resp
        )
    )

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
            "domain": "automation",
            "local_hash": "h1",
        },
        "/config/blueprints/b2.yaml": {
            "name": "B2",
            "rel_path": "b2.yaml",
            "source_url": "https://url/b2",
            "domain": "automation",
            "local_hash": "h2",
        },
    }

    mock_503_resp = MagicMock(spec=httpx.Response)
    mock_503_resp.status_code = 503
    mock_503_resp.headers = {}
    mock_503_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "503 Service Unavailable", request=MagicMock(), response=mock_503_resp
        )
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
    mock_resp_empty.headers = {}
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
    mock_resp_invalid.headers = {}
    mock_resp_invalid.raise_for_status = MagicMock()
    mock_resp_invalid.text = "}invalid yaml: {\n"
    mock_session.get.return_value = mock_resp_invalid

    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )
    assert "yaml_syntax_error" in str(coordinator.data[path]["last_error"])

    mock_resp_missing_bp = MagicMock(spec=httpx.Response)
    mock_resp_missing_bp.status_code = 200
    mock_resp_missing_bp.headers = {}
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
            "domain": "automation",
            "local_hash": "old",
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
async def test_async_update_blueprint_unsafe_url_invalidates_cache(coordinator):
    """Test that an unsafe source URL invalidates the cached remote metadata."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {
        path: {
            "name": "Test",
            "source_url": "http://malicious.com",
            "remote_hash": "old_hash",
            "remote_content": "old_content",
            "etag": "old_etag",
            "updatable": True,
            "last_error": None,
        }
    }

    with (
        patch.object(coordinator, "_is_safe_url", return_value=False),
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=MagicMock(),
        ),
    ):
        await coordinator.async_fetch_blueprint(path)

    assert coordinator.data[path]["remote_hash"] is None
    assert coordinator.data[path]["remote_content"] is None
    assert coordinator.data[path]["etag"] is None
    assert coordinator.data[path]["updatable"] is False
    assert "unsafe_url|" in str(coordinator.data[path]["last_error"])


@pytest.mark.asyncio
async def test_async_background_refresh_cancellation_stops_workers(coordinator):
    """Test that cancelling the background refresh task stops workers promptly."""
    num_blueprints = 100
    blueprints = {
        f"/bp{i}.yaml": {
            "name": f"BP{i}",
            "rel_path": f"bp{i}.yaml",
            "source_url": f"https://url/bp{i}",
            "domain": "automation",
            "local_hash": "h",
        }
        for i in range(num_blueprints)
    }

    processed_count = 0

    async def slow_update(*_args, **_kwargs):
        """Slow update to ensure we can cancel while processing."""
        nonlocal processed_count
        processed_count += 1
        await asyncio.sleep(0.2)

    with (
        patch.object(coordinator, "_async_update_blueprint_in_place", side_effect=slow_update),
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=MagicMock(),
        ),
    ):
        task = asyncio.create_task(coordinator._async_background_refresh(blueprints))

        await asyncio.sleep(0.3)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert processed_count < num_blueprints


@pytest.mark.asyncio
async def test_async_update_data_auto_update_multiple_sorted(coordinator):
    """Test _async_update_data sorts multiple auto-updated blueprints."""
    coordinator.config_entry.options = MappingProxyType({"auto_update": True})
    blueprints = {
        "/b.yaml": {
            "name": "Beta",
            "rel_path": "b.yaml",
            "source_url": "https://url/b",
            "domain": "automation",
            "local_hash": "old",
        },
        "/a.yaml": {
            "name": "Alpha",
            "rel_path": "a.yaml",
            "source_url": "https://url/a",
            "domain": "automation",
            "local_hash": "old",
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
async def test_async_restore_blueprint_success(hass, coordinator):
    """Test successful restoration of a blueprint backup."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.realpath",
            side_effect=os.path.normpath,
        ),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
        patch("custom_components.blueprints_updater.coordinator.os.rename"),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2"),
    ):
        result = await coordinator.async_restore_blueprint(path)

    mock_replace.assert_any_call(
        os.path.normpath(f"{path}.bak.1"), os.path.normpath(f"{path}.bak.2")
    )
    assert result["success"] is True
    assert result["translation_key"] == "success"
    hass.services.async_call.assert_any_call("automation", "reload")


@pytest.mark.asyncio
async def test_async_restore_blueprint_missing(hass, coordinator):
    """Test restoration when backup is missing."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
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
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
        patch("custom_components.blueprints_updater.coordinator.os.rename"),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2"),
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
    assert (tmp_path / "test.yaml.bak.2").read_text() == "backup_v1"


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


@pytest.mark.asyncio
async def test_async_fetch_content_retry_limit(coordinator):
    """Test that _async_fetch_content retries exactly MAX_RETRIES times."""
    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(side_effect=httpx.RequestError("Fetch failed"))

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        pytest.raises(httpx.RequestError, match="Fetch failed"),
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

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.time.monotonic",
            side_effect=[100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7] + [100.8] * 100,
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

        expected_delay = (100.1 + MIN_SEND_INTERVAL) - 100.2
        mock_sleep.assert_called_with(pytest.approx(expected_delay))


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

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.time.monotonic",
            side_effect=[200.0, 200.1, 200.2, 200.3, 200.4, 200.5, 200.6, 200.7] + [200.8] * 100,
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

        expected_delay = (200.1 + MAX_SEND_INTERVAL) - 200.2
        mock_sleep.assert_called_with(pytest.approx(expected_delay))


@pytest.mark.asyncio
async def test_async_reload_services_whitelist(coordinator):
    """Test that only whitelisted domains are reloaded."""
    coordinator.hass.services.has_service = MagicMock(return_value=True)
    coordinator.hass.services.async_call = AsyncMock()

    await coordinator.async_reload_services(["automation"])
    coordinator.hass.services.async_call.assert_called_once_with("automation", "reload")
    coordinator.hass.services.async_call.reset_mock()

    await coordinator.async_reload_services(["malicious_service"])
    coordinator.hass.services.async_call.assert_not_called()

    await coordinator.async_reload_services(["script", "invalid"])
    coordinator.hass.services.async_call.assert_called_once_with("script", "reload")


@pytest.mark.asyncio
async def test_async_install_blueprint_targeted_reload(coordinator):
    """Test that installing a blueprint with a specific domain only reloads that domain."""
    path = "/config/blueprints/script.yaml"
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


def test_scan_blueprints_domain_extraction(hass, coordinator):
    """Test that domain is extracted during blueprint scan."""
    bp_path = "/config/blueprints"
    mock_files = [(bp_path, [], ["script.yaml", "automation.yaml"])]

    contents = {
        "script.yaml": "blueprint:\n  name: S\n  domain: script\n  source_url: url",
        "automation.yaml": "blueprint:\n  name: A\n  source_url: url",
    }

    def open_side_effect(path, *_args, **_kwargs):
        """Mock open_side_effect."""
        content = contents.get(os.path.basename(str(path)), "")
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

        script_path = next(k for k in results if "script.yaml" in k)
        auto_path = next(k for k in results if "automation.yaml" in k)

        assert results[script_path]["domain"] == "script"
        assert results[auto_path]["domain"] == "automation"


def test_is_safe_path(coordinator):
    """Test _is_safe_path logic."""
    coordinator._is_safe_path = BlueprintUpdateCoordinator._is_safe_path.__get__(coordinator)
    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.realpath",
        side_effect=os.path.normpath,
    ):
        assert coordinator._is_safe_path("/config/blueprints/test.yaml")
        assert coordinator._is_safe_path("/config/blueprints/automation/test.yaml")
        assert not coordinator._is_safe_path("/config/secrets.yaml")
        assert not coordinator._is_safe_path("/etc/passwd")
        assert not coordinator._is_safe_path("/config/blueprints/../secrets.yaml")


@pytest.mark.asyncio
async def test_is_safe_url(coordinator):
    """Test _is_safe_url logic."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    coord: Any = coordinator

    assert await coord._is_safe_url("https://github.com/user/repo")
    assert await coord._is_safe_url("https://raw.githubusercontent.com/user/repo/main/bp.yaml")
    assert await coord._is_safe_url("https://gist.github.com/user/gistid")
    assert await coord._is_safe_url("https://community.home-assistant.io/t/topic/123")
    assert await coord._is_safe_url("https://gitlab.com/user/repo/-/raw/main/bp.yaml")
    assert await coord._is_safe_url("https://bitbucket.org/user/repo/raw/main/bp.yaml")

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
async def test_async_restore_blueprint_unsafe_path(coordinator):
    """Test that restoring to an unsafe path is blocked."""
    coordinator._is_safe_path = BlueprintUpdateCoordinator._is_safe_path.__get__(coordinator)
    result = await coordinator.async_restore_blueprint("/config/secrets.yaml")
    assert result["success"] is False
    assert result["translation_key"] == "system_error"


@pytest.mark.asyncio
async def test_async_update_blueprint_in_place_unsafe_url(coordinator):
    """Test that updating from an unsafe URL is blocked."""
    coordinator._is_safe_url = BlueprintUpdateCoordinator._is_safe_url.__get__(coordinator)
    path = "/config/blueprints/test.yaml"
    info = {"source_url": "http://192.168.1.1/exploit", "domain": "automation"}

    with patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger:
        await coordinator._async_update_blueprint_in_place(MagicMock(), path, info, [], set())
        mock_logger.warning.assert_called_with(
            "Blocking update from untrusted URL: %s", "http://192.168.1.1/exploit"
        )


@pytest.mark.asyncio
async def test_async_fetch_blueprint_regression_key_error_hash(coordinator):
    """Regression test for KeyError: 'hash' when fetching on-demand.

    This ensures that when async_fetch_blueprint is called (e.g. from update.py),
    it correctly handles the 'local_hash' key instead of crashing on 'hash'.
    """
    path = "/config/blueprints/automation/test.yaml"

    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": "https://github.com/user/repo/blob/main/test.yaml",
            "local_hash": "old_hash",
            "updatable": True,
            "remote_hash": None,
            "remote_content": None,
            "last_error": None,
            "etag": None,
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_etag"}
    mock_response.text = "blueprint:\n  name: Test"
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_async_client",
            return_value=mock_session,
        ),
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_hash.return_value.hexdigest.return_value = "new_hash"
        await coordinator.async_fetch_blueprint(path)

    assert coordinator.data[path]["remote_hash"] == "new_hash"
    assert coordinator.data[path]["etag"] == "new_etag"
    assert coordinator.data[path]["updatable"] is True


@pytest.mark.asyncio
async def test_metadata_pruning(coordinator):
    """Test that stale metadata is pruned during update."""
    path_valid = "/config/blueprints/valid.yaml"
    path_stale = "/config/blueprints/stale.yaml"

    coordinator._persisted_etags = {path_valid: "etag1", path_stale: "etag2"}
    coordinator._persisted_hashes = {path_valid: "hash1", path_stale: "hash2"}

    blueprints = {
        path_valid: {
            "name": "Valid",
            "rel_path": "valid.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": "hash1",
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()

    assert path_valid in coordinator._persisted_etags
    assert path_stale not in coordinator._persisted_etags
    assert path_valid in coordinator._persisted_hashes
    assert path_stale not in coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_async_save_metadata_empty_data(coordinator):
    """Test that saving metadata with empty data clears the store."""
    coordinator.data = {}
    coordinator.setup_complete = True
    coordinator._persisted_etags = {"stale": "etag"}
    coordinator._persisted_hashes = {"stale": "hash"}

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock) as mock_save:
        await coordinator._async_save_metadata()

    mock_save.assert_called_once_with({"etags": {}, "remote_hashes": {}})
    assert not coordinator._persisted_etags
    assert not coordinator._persisted_hashes


@pytest.mark.asyncio
async def test_async_fetch_content_pacing_synchronization(coordinator):
    """Test that multiple concurrent requests result in strictly increasing _last_request_time."""
    coordinator._last_request_time = 100.0

    async_client = httpx.AsyncClient()
    try:
        with (
            patch(
                "custom_components.blueprints_updater.coordinator.time.monotonic",
                side_effect=[105.0, 105.1, 105.2] + [105.3] * 100,
            ),
            patch(
                "custom_components.blueprints_updater.coordinator.random.uniform",
                return_value=1.0,
            ),
            patch(
                "custom_components.blueprints_updater.coordinator.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            patch.object(async_client, "get", new_callable=AsyncMock) as mock_get,
        ):
            mock_get.return_value = MagicMock(spec=httpx.Response)
            mock_get.return_value.status_code = 200
            mock_get.return_value.headers = {"ETag": "new"}
            mock_get.return_value.text = "blueprint:\n  name: Test"
            mock_get.return_value.raise_for_status = MagicMock()

            tasks = [
                coordinator._async_fetch_content(async_client, "https://url1/bp.yaml"),
                coordinator._async_fetch_content(async_client, "https://url2/bp.yaml"),
                coordinator._async_fetch_content(async_client, "https://url3/bp.yaml"),
            ]

            await asyncio.gather(*tasks)

            assert coordinator._last_request_time >= 107.0

            sleep_args = [round(call.args[0], 1) for call in mock_sleep.call_args_list]
            assert len(sleep_args) == 2
            assert all(d > 0 for d in sleep_args)
    finally:
        await async_client.aclose()


def test_ensure_source_url_structured_modification():
    """Test that _ensure_source_url prefers structured YAML modification."""
    content = "blueprint:\n  name: Test\n"
    source_url = "https://example.com/bp.yaml"

    result = BlueprintUpdateCoordinator._ensure_source_url(content, source_url)
    assert "source_url: https://example.com/bp.yaml" in result

    parsed = yaml_util.parse_yaml(result)
    assert isinstance(parsed, dict)
    assert isinstance(parsed["blueprint"], dict)
    assert parsed["blueprint"]["source_url"] == source_url
    assert parsed["blueprint"]["name"] == "Test"


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
async def test_async_setup_sanitization(hass, coordinator):
    """Test that async_setup sanitizes corrupted or invalid storage data."""
    mock_store = MagicMock()
    coordinator._store = mock_store

    mock_store.async_load = AsyncMock(
        return_value={
            "etags": "not_a_dict",
            "remote_hashes": ["not", "a", "dict"],
        }
    )

    with patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn:
        await coordinator.async_setup()
        assert coordinator._persisted_etags == {}
        assert coordinator._persisted_hashes == {}
        assert coordinator.setup_complete
        assert mock_warn.call_count == 2
        warn_msgs = [call.args[0] for call in mock_warn.call_args_list]
        assert any("Ignoring invalid persisted etags" in msg for msg in warn_msgs)
        assert any("Ignoring invalid persisted remote_hashes" in msg for msg in warn_msgs)

    mock_store.async_load = AsyncMock(
        return_value={
            "etags": {
                "valid_key": "valid_value",
                "invalid_key": 123,
                456: "invalid_val",
            },
            "remote_hashes": {
                "valid_hash_key": "hash_val",
                "broken": None,
            },
        }
    )

    coordinator.setup_complete = False
    with patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn:
        await coordinator.async_setup()
        assert coordinator._persisted_etags == {"valid_key": "valid_value"}
        assert coordinator._persisted_hashes == {"valid_hash_key": "hash_val"}
        assert coordinator.setup_complete
        assert mock_warn.call_count == 2

        mock_warn.assert_any_call(
            "Dropped %d invalid ETag entries from storage (non-string keys or values)", 2
        )
        mock_warn.assert_any_call("Dropped %d invalid remote hash entries from storage", 1)


@pytest.mark.asyncio
async def test_process_blueprint_content_yaml_error(coordinator):
    """Test handling of YAML syntax error during content processing."""
    path = "/config/blueprints/test.yaml"
    info = {"name": "Test", "local_hash": "hash"}
    coordinator.data = {path: info}

    with patch(
        "custom_components.blueprints_updater.coordinator.yaml_util.parse_yaml",
        side_effect=HomeAssistantError("Invalid YAML"),
    ):
        await coordinator._process_blueprint_content(
            path, info, "invalid", None, "https://url", [], set()
        )

    assert "yaml_syntax_error|Invalid YAML" in coordinator.data[path]["last_error"]


@pytest.mark.asyncio
async def test_process_blueprint_content_unhandled_error(coordinator):
    """Test that non-HomeAssistantErrors propagate during content processing."""
    path = "/config/blueprints/test.yaml"
    info = {"name": "Test", "local_hash": "hash"}
    coordinator.data = {path: info}

    with (
        patch.object(
            BlueprintUpdateCoordinator, "_ensure_source_url", side_effect=lambda content, _: content
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.yaml_util.parse_yaml",
            side_effect=ValueError("Unexpected error"),
        ),
        pytest.raises(ValueError, match="Unexpected error"),
    ):
        await coordinator._process_blueprint_content(
            path, info, "invalid", None, "https://url", [], set()
        )


@pytest.mark.asyncio
async def test_async_install_blueprint_yaml_error_logging(coordinator):
    """Test that YAML errors during install reload are logged as warnings."""
    path = "/config/blueprints/test.yaml"
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


def test_get_validated_selected_blueprints_hardening(coordinator):
    """Test the hardening of _get_validated_selected_blueprints."""
    assert coordinator._get_validated_selected_blueprints(None) == []

    res = coordinator._get_validated_selected_blueprints("  path/to/bp.yaml  ")
    assert res == ["path/to/bp.yaml"]
    assert coordinator._get_validated_selected_blueprints("   ") == []
    assert coordinator._get_validated_selected_blueprints(["a", " b ", None, ""]) == ["a", "b"]
    assert coordinator._get_validated_selected_blueprints(("a", "b")) == ["a", "b"]

    with patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger:
        assert coordinator._get_validated_selected_blueprints({"key": "value"}) == []
        mock_logger.error.assert_called()
        assert "mapping" in mock_logger.error.call_args[0][0]
    with patch("custom_components.blueprints_updater.coordinator._LOGGER") as mock_logger:
        assert coordinator._get_validated_selected_blueprints(123) == []
        mock_logger.error.assert_called()
        assert "Invalid type" in mock_logger.error.call_args[0][0]


def test_get_validated_filter_mode_normalization(coordinator):
    """Test that filter mode is normalized (lowercase and stripped)."""
    assert coordinator._get_validated_filter_mode("  All  ") == "all"
    assert coordinator._get_validated_filter_mode("WHITELIST") == "whitelist"
    assert coordinator._get_validated_filter_mode("Blacklist") == "blacklist"
    assert coordinator._get_validated_filter_mode("invalid") == "all"
    assert coordinator._get_validated_filter_mode(None) == "all"
    assert coordinator._get_validated_filter_mode(123) == "all"


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


def test_get_cached_git_diff(coordinator):
    """Test get_cached_git_diff logic."""
    path = "test.yaml"
    coordinator.data = {path: {"_cached_git_diff": ("local", "remote", "diff")}}
    assert coordinator.get_cached_git_diff(path, "local", "remote") == "diff"
    assert coordinator.get_cached_git_diff(path, "wrong", "remote") is None
    assert coordinator.get_cached_git_diff("missing", "local", "remote") is None


def test_set_cached_git_diff(coordinator):
    """Test set_cached_git_diff logic."""
    path = "test.yaml"
    coordinator.data = {path: {}}
    coordinator.set_cached_git_diff(path, "l1", "r1", "d1")
    assert coordinator.data[path]["_cached_git_diff"] == ("l1", "r1", "d1")


@pytest.mark.asyncio
async def test_async_get_git_diff_cache_hit(coordinator):
    """Test async_get_git_diff returns cached value if hashes match."""
    path = "test.yaml"
    coordinator.data = {
        path: {
            "local_hash": "h1",
            "remote_hash": "h2",
            "_cached_git_diff": ("h1", "h2", "cached_diff"),
        }
    }
    with patch.object(coordinator, "async_fetch_diff_content") as mock_fetch:
        diff = await coordinator.async_get_git_diff(path)
        assert diff == "cached_diff"
        mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_async_get_git_diff_full_flow(coordinator):
    """Test async_get_git_diff fetches and generates diff on cache miss."""
    path = "test.yaml"
    coordinator.data = {
        path: {
            "local_hash": "h1",
            "remote_hash": "h2",
            "source_url": "https://url.com",
            "updatable": True,
        }
    }

    local_content = "blueprint:\n  name: Old"
    remote_content = "blueprint:\n  name: New"

    with (
        patch.object(coordinator, "async_fetch_diff_content", return_value=remote_content),
        patch("builtins.open", mock_open(read_data=local_content)),
    ):
        diff = await coordinator.async_get_git_diff(path)
        assert diff is not None
        assert "+  name: New" in diff
        assert coordinator.data[path]["_cached_git_diff"] == ("h1", "h2", diff)
