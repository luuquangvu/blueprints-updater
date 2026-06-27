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
