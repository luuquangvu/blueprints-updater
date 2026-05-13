"""Tests for Blueprints Updater Last-Modified logic."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord.config_entry = entry
        coord.data = {}
        return coord


@pytest.mark.asyncio
async def test_async_fetch_content_sends_if_modified_since(coordinator):
    """Test that _async_fetch_content sends If-Modified-Since header."""
    session = MagicMock(spec=httpx.AsyncClient)
    last_modified = "Mon, 10 May 2021 10:00:00 GMT"

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers(
        {"Last-Modified": last_modified, "Content-Type": "text/yaml"}
    )
    mock_response.text = "content"
    mock_response.url = httpx.URL("https://url")

    with (
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=mock_response)
        ) as mock_exec,
        patch.object(coordinator, "_apply_request_pacing", AsyncMock()),
    ):
        await coordinator._async_fetch_content(session, "https://url", last_modified=last_modified)

        mock_exec.assert_awaited_once()
        args, _kwargs = mock_exec.call_args
        headers = args[2]
        assert headers.get("If-Modified-Since") == last_modified


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_last_modified_propagation(coordinator):
    """Test propagation of Last-Modified in CDN fallback logic."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"
    last_modified_hdr = "Mon, 10 May 2021 10:00:00 GMT"

    # First call: no last_modified
    coordinator._async_fetch_content = AsyncMock(return_value=("content", "etag", None))

    content, etag, last_modified = await coordinator._async_fetch_with_cdn_fallback(
        session, "path", normalized_url, cdn_url, None, None, None, False
    )

    assert content == "content"
    assert etag == "etag"
    assert last_modified is None
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag=None, last_modified=None, force=False
    )

    # Second call: last_modified is set
    coordinator._async_fetch_content.reset_mock()
    coordinator._async_fetch_content.return_value = ("content-2", "etag-2", last_modified_hdr)

    content2, etag2, last_modified2 = await coordinator._async_fetch_with_cdn_fallback(
        session,
        "path",
        normalized_url,
        cdn_url,
        None,
        last_modified_hdr,
        "remote_hash",
        False,
    )

    assert content2 == "content-2"
    assert etag2 == "etag-2"
    assert last_modified2 == last_modified_hdr
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag=None, last_modified=last_modified_hdr, force=False
    )
