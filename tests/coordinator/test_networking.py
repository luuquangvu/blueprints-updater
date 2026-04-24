"""Tests for coordinator networking, fetching, and CDN logic."""

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.blueprints_updater.const import (
    MAX_RETRIES,
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
)


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
    mock_response.status_code = HTTPStatus.OK
    mock_response.is_redirect = False
    mock_response.url = httpx.URL("https://example.com/path")
    mock_response.text = "content"
    mock_response.headers = {"Content-Type": "text/yaml"}
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
    mock_response.status_code = HTTPStatus.OK
    mock_response.is_redirect = False
    mock_response.url = httpx.URL("https://example.com/path")
    mock_response.text = "content"
    mock_response.headers = {"Content-Type": "text/yaml"}
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
            mock_get.return_value.status_code = HTTPStatus.OK
            mock_get.return_value.is_redirect = False
            mock_get.return_value.url = httpx.URL("https://example.com/path")
            mock_get.return_value.headers = {"ETag": "new", "Content-Type": "text/yaml"}
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


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_security(coordinator):
    """Test security protections in redirect guard."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_resp_redirect = MagicMock(spec=httpx.Response)
    mock_resp_redirect.status_code = HTTPStatus.FOUND
    mock_resp_redirect.is_redirect = True
    mock_resp_redirect.headers = {"Location": "https://example.com/next"}
    mock_resp_redirect.url = httpx.URL("https://example.com/start")

    mock_session.get = AsyncMock(return_value=mock_resp_redirect)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=True),
        pytest.raises(httpx.HTTPError, match="Too many redirects"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "https://example.com/start", {}
        )

    mock_resp_unsafe = MagicMock(spec=httpx.Response)
    mock_resp_unsafe.status_code = HTTPStatus.FOUND
    mock_resp_unsafe.is_redirect = True
    mock_resp_unsafe.headers = {"Location": "http://unsafe.com"}
    mock_resp_unsafe.url = httpx.URL("https://example.com/start")

    mock_session.get = AsyncMock(return_value=mock_resp_unsafe)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=False),
        pytest.raises(httpx.HTTPError, match="Security violation"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "https://example.com/start", {}
        )


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_final_https(coordinator):
    """Test that the redirect guard enforces HTTPS for the final destination."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_resp_final_safe = MagicMock()
    mock_resp_final_safe.status_code = HTTPStatus.OK
    mock_resp_final_safe.is_redirect = False
    mock_resp_final_safe.url = httpx.URL("https://safe.com/bp.yaml")
    mock_resp_final_safe.raise_for_status = MagicMock()

    mock_session.get = AsyncMock(return_value=mock_resp_final_safe)

    with patch.object(coordinator, "_is_safe_url", return_value=True):
        resp = await coordinator._execute_with_redirect_guard(
            mock_session, "http://start.com/bp.yaml", {}
        )
        assert str(resp.url) == "https://safe.com/bp.yaml"

    mock_resp_final_unsafe = MagicMock()
    mock_resp_final_unsafe.status_code = HTTPStatus.OK
    mock_resp_final_unsafe.is_redirect = False
    mock_resp_final_unsafe.url = httpx.URL("http://unsafe.com/bp.yaml")
    mock_resp_final_unsafe.raise_for_status = MagicMock()

    mock_session.get = AsyncMock(return_value=mock_resp_final_unsafe)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=True),
        pytest.raises(httpx.HTTPError, match="must be HTTPS"),
    ):
        await coordinator._execute_with_redirect_guard(mock_session, "http://start.com/bp.yaml", {})


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_304_non_redirect_handling(coordinator):
    """Test that 304 responses are handled correctly even if not flagged as redirects."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.NOT_MODIFIED
    mock_response.is_redirect = False
    mock_response.url = httpx.URL("https://example.com/bp.yaml")
    mock_response.headers = httpx.Headers({"ETag": "test-etag"})
    mock_response.raise_for_status = MagicMock()

    mock_session.get = AsyncMock(return_value=mock_response)

    result = await coordinator._execute_with_redirect_guard(
        mock_session, "https://example.com/bp.yaml", {}
    )

    mock_response.raise_for_status.assert_not_called()
    assert result is mock_response


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_304_handling(coordinator):
    """Test that 304 responses are handled correctly even if flagged as redirects."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.NOT_MODIFIED
    mock_response.is_redirect = True
    mock_response.url = httpx.URL("https://example.com/bp.yaml")
    mock_response.headers = httpx.Headers({"ETag": "test-etag"})
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "Redirect error", request=MagicMock(), response=mock_response
        )
    )

    mock_session.get = AsyncMock(return_value=mock_response)

    with patch.object(coordinator, "_is_safe_url", return_value=True):
        resp = await coordinator._execute_with_redirect_guard(
            mock_session, "https://example.com/bp.yaml", {}
        )
        assert resp.status_code == HTTPStatus.NOT_MODIFIED
        mock_response.raise_for_status.assert_not_called()


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_304_https_enforcement(coordinator):
    """Test that 304 responses are still subject to HTTPS enforcement."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.NOT_MODIFIED
    mock_response.is_redirect = True
    mock_response.url = httpx.URL("http://unsafe.com/bp.yaml")

    mock_session.get = AsyncMock(return_value=mock_response)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=True),
        pytest.raises(httpx.HTTPError, match="must be HTTPS"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "http://unsafe.com/bp.yaml", {}
        )
