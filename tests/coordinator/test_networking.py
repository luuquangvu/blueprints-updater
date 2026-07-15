"""Tests for coordinator networking and fetching logic."""

import asyncio
import socket
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import (
    DOMAIN,
    MAX_RESPONSE_BYTES,
    MAX_RETRIES,
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.network import (
    SSL_ALPN_HTTP11,
    GuardedAsyncHTTPTransport,
    SafeAsyncNetworkBackend,
    _client_ssl_context,
    async_resolve_public_addresses,
    get_guarded_async_client,
    normalize_hostname,
)


class _ChunkedStream(httpx.AsyncByteStream):
    """Test stream that yields predefined response chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Initialize the stream with response chunks."""
        self._chunks = chunks
        self._index = 0

    def __aiter__(self):
        """Return the asynchronous chunk iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Return the next configured chunk."""
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


def test_legacy_ssl_context_omits_unsupported_alpn_argument():
    """Legacy Home Assistant SSL helpers are called without the newer ALPN keyword."""
    expected_context = MagicMock()
    with (
        patch(
            "custom_components.blueprints_updater.network._HAS_NATIVE_ALPN_SUPPORT",
            False,
        ),
        patch(
            "custom_components.blueprints_updater.network.ha_ssl.client_context",
            return_value=expected_context,
        ) as mock_client_context,
    ):
        assert _client_ssl_context(("http/1.1",)) is expected_context

    mock_client_context.assert_called_once_with()


def test_legacy_guarded_client_omits_unsupported_alpn_argument(hass):
    """Legacy Home Assistant HTTPX factories receive only supported keywords."""
    hass.data.setdefault(DOMAIN, {}).pop("guarded_http_clients", None)
    expected_client = MagicMock(spec=httpx.AsyncClient)
    with (
        patch(
            "custom_components.blueprints_updater.network._HAS_NATIVE_ALPN_SUPPORT",
            False,
        ),
        patch(
            "custom_components.blueprints_updater.network.GuardedAsyncHTTPTransport"
        ) as mock_transport,
        patch(
            "custom_components.blueprints_updater.network.create_async_httpx_client",
            return_value=expected_client,
        ) as mock_create_client,
    ):
        assert get_guarded_async_client(hass, alpn_protocols=None) is expected_client

    mock_transport.assert_called_once_with(hass, http2=False)
    mock_create_client.assert_called_once_with(
        hass,
        http2=False,
        transport=mock_transport.return_value,
    )


def test_guarded_client_normalizes_unsupported_alpn_to_http11(hass):
    """Unsupported custom ALPN tuples use the documented HTTP/1.1 mode."""
    hass.data.setdefault(DOMAIN, {}).pop("guarded_http_clients", None)
    expected_client = MagicMock(spec=httpx.AsyncClient)
    with (
        patch(
            "custom_components.blueprints_updater.network._HAS_NATIVE_ALPN_SUPPORT",
            True,
        ),
        patch(
            "custom_components.blueprints_updater.network.GuardedAsyncHTTPTransport"
        ) as mock_transport,
        patch(
            "custom_components.blueprints_updater.network.create_async_httpx_client",
            return_value=expected_client,
        ) as mock_create_client,
    ):
        assert get_guarded_async_client(hass, alpn_protocols=("custom",)) is expected_client

    mock_transport.assert_called_once_with(hass, http2=False)
    mock_create_client.assert_called_once_with(
        hass,
        alpn_protocols=SSL_ALPN_HTTP11,
        http2=False,
        transport=mock_transport.return_value,
    )


def test_guarded_transport_wires_backend_through_pool_constructor(hass):
    """The guarded backend is supplied through httpcore's public pool API."""
    ssl_context = MagicMock()
    backend = MagicMock(spec=SafeAsyncNetworkBackend)
    pool = MagicMock(spec=httpcore.AsyncConnectionPool)

    with (
        patch(
            "custom_components.blueprints_updater.network._client_ssl_context",
            return_value=ssl_context,
        ),
        patch(
            "custom_components.blueprints_updater.network.SafeAsyncNetworkBackend",
            return_value=backend,
        ),
        patch(
            "custom_components.blueprints_updater.network.httpcore.AsyncConnectionPool",
            return_value=pool,
        ) as pool_type,
    ):
        transport = GuardedAsyncHTTPTransport(hass, http2=True)

    pool_type.assert_called_once_with(
        ssl_context=ssl_context,
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=5.0,
        http1=True,
        http2=True,
        network_backend=backend,
    )
    assert transport._pool is pool


@pytest.mark.asyncio
async def test_guarded_transport_adapts_response_and_maps_errors(hass):
    """The custom transport preserves httpx responses and exception classes."""
    transport = GuardedAsyncHTTPTransport(hass, http2=False)
    pool = MagicMock(spec=httpcore.AsyncConnectionPool)
    transport._pool = pool
    request = httpx.Request(
        "GET",
        "https://example.com/blueprint.yaml",
        stream=_ChunkedStream([]),
    )
    response_stream = _ChunkedStream([b"blueprint"])
    pool.handle_async_request = AsyncMock(
        return_value=httpcore.Response(
            200,
            headers=[(b"content-type", b"text/yaml")],
            content=response_stream,
        )
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert await response.aread() == b"blueprint"

    pool.handle_async_request.side_effect = httpcore.ConnectError("blocked")
    with pytest.raises(httpx.ConnectError, match="blocked"):
        await transport.handle_async_request(request)

    class FutureConnectError(httpcore.ConnectError):
        """Represent a future httpcore subtype unknown to this integration."""

    pool.handle_async_request.side_effect = FutureConnectError("future failure")
    with pytest.raises(httpx.ConnectError, match="future failure"):
        await transport.handle_async_request(request)

    await transport.aclose()
    pool.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_special_use_suffix_check_respects_dns_label_boundary(hass):
    """A reserved word outside the final DNS suffix remains publicly resolvable."""
    hass.async_add_executor_job = AsyncMock(
        return_value=[
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]
    )

    assert await async_resolve_public_addresses(hass, "my-local.example.com", 443) == ("1.1.1.1",)
    hass.async_add_executor_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_special_use_suffix_check_supports_multi_label_suffix(hass):
    """The complete multi-label home.arpa suffix is rejected before DNS resolution."""
    hass.async_add_executor_job = AsyncMock()

    assert await async_resolve_public_addresses(hass, "DEVICE.HOME.ARPA.", 443) == ()
    hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("BÜCHER.Example.", "xn--bcher-kva.example"),
        ("XN--BCHER-KVA.EXAMPLE", "xn--bcher-kva.example"),
        ("example.com..", None),
        ("bad host.example", None),
        ("", None),
    ],
)
def test_hostname_normalization(hostname, expected):
    """Hostnames are canonicalized to strict ASCII before safety checks."""
    assert normalize_hostname(hostname) == expected


@pytest.mark.asyncio
async def test_public_resolver_uses_canonical_idna_hostname(hass):
    """DNS resolution receives the same canonical hostname used by URL preflight."""
    hass.async_add_executor_job = AsyncMock(
        return_value=[
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]
    )

    assert await async_resolve_public_addresses(hass, "BÜCHER.com.", 443) == ("1.1.1.1",)
    hass.async_add_executor_job.assert_awaited_once_with(
        socket.getaddrinfo,
        "xn--bcher-kva.com",
        443,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )


@pytest.mark.asyncio
async def test_safe_backend_rechecks_dns_at_connection_time(hass):
    """A private connection-time answer is rejected even after a public preflight."""
    backend = SafeAsyncNetworkBackend(hass)
    backend._backend = MagicMock()

    with (
        patch(
            "custom_components.blueprints_updater.network.async_resolve_public_addresses",
            AsyncMock(return_value=()),
        ),
        pytest.raises(httpcore.ConnectError, match="Unsafe or unresolvable destination"),
    ):
        await backend.connect_tcp("rebind.example.net", 443)

    backend._backend.connect_tcp.assert_not_called()


def test_safe_backend_uses_public_anyio_backend(hass):
    """The guarded backend avoids private httpcore backend modules."""
    assert isinstance(SafeAsyncNetworkBackend(hass)._backend, httpcore.AnyIOBackend)


@pytest.mark.asyncio
async def test_safe_backend_shares_timeout_across_addresses(hass):
    """Every address attempt consumes one shared connection timeout budget."""
    backend = SafeAsyncNetworkBackend(hass)
    expected_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
    network_backend = MagicMock(spec=httpcore.AsyncNetworkBackend)
    network_backend.connect_tcp = AsyncMock(
        side_effect=[httpcore.ConnectError("first failed"), expected_stream]
    )
    backend._backend = network_backend

    with (
        patch(
            "custom_components.blueprints_updater.network.async_resolve_public_addresses",
            AsyncMock(return_value=("1.1.1.1", "8.8.8.8")),
        ),
        patch(
            "custom_components.blueprints_updater.network.time.monotonic",
            side_effect=[100.0, 101.0, 103.5],
        ),
    ):
        result = await backend.connect_tcp("example.com", 443, timeout=5.0)

    assert result is expected_stream
    assert [call.kwargs["timeout"] for call in network_backend.connect_tcp.call_args_list] == [
        4.0,
        1.5,
    ]


@pytest.mark.asyncio
async def test_safe_backend_preserves_unlimited_timeout(hass):
    """An unlimited connection timeout remains unlimited for every address."""
    backend = SafeAsyncNetworkBackend(hass)
    expected_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
    network_backend = MagicMock(spec=httpcore.AsyncNetworkBackend)
    network_backend.connect_tcp = AsyncMock(
        side_effect=[httpcore.ConnectError("first failed"), expected_stream]
    )
    backend._backend = network_backend

    with (
        patch(
            "custom_components.blueprints_updater.network.async_resolve_public_addresses",
            AsyncMock(return_value=("1.1.1.1", "8.8.8.8")),
        ),
        patch("custom_components.blueprints_updater.network.time.monotonic") as monotonic,
    ):
        result = await backend.connect_tcp("example.com", 443, timeout=None)

    assert result is expected_stream
    monotonic.assert_not_called()
    assert [call.kwargs["timeout"] for call in network_backend.connect_tcp.call_args_list] == [
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_safe_backend_stops_when_shared_timeout_is_exhausted(hass):
    """No additional address is attempted after the shared budget expires."""
    backend = SafeAsyncNetworkBackend(hass)
    first_error = httpcore.ConnectError("first failed")
    network_backend = MagicMock(spec=httpcore.AsyncNetworkBackend)
    network_backend.connect_tcp = AsyncMock(side_effect=first_error)
    backend._backend = network_backend

    with (
        patch(
            "custom_components.blueprints_updater.network.async_resolve_public_addresses",
            AsyncMock(return_value=("1.1.1.1", "8.8.8.8")),
        ),
        patch(
            "custom_components.blueprints_updater.network.time.monotonic",
            side_effect=[100.0, 101.0, 106.0],
        ),
        pytest.raises(httpcore.ConnectError, match="Unable to connect") as exc_info,
    ):
        await backend.connect_tcp("example.com", 443, timeout=5.0)

    network_backend.connect_tcp.assert_awaited_once()
    assert exc_info.value.__cause__ is first_error


@pytest.mark.asyncio
async def test_bounded_response_rejects_declared_and_streamed_oversize(coordinator):
    """Declared and actual response bodies cannot cross the byte ceiling."""

    async def declared_handler(request: httpx.Request) -> httpx.Response:
        """Return a response whose declared size is excessive."""
        return httpx.Response(
            200,
            headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
            content=b"small",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(declared_handler)) as client:
        with pytest.raises(httpx.HTTPError, match="exceeds"):
            await BlueprintUpdateCoordinator._async_get_bounded_response(
                client, "https://example.com", {}
            )

    async def streamed_handler(request: httpx.Request) -> httpx.Response:
        """Return chunks that cross the decoded-content ceiling."""
        return httpx.Response(
            200,
            stream=_ChunkedStream([b"a" * MAX_RESPONSE_BYTES, b"b"]),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(streamed_handler)) as client:
        with pytest.raises(httpx.HTTPError, match="exceeds"):
            await BlueprintUpdateCoordinator._async_get_bounded_response(
                client, "https://example.com", {}
            )


@pytest.mark.asyncio
async def test_bounded_response_accepts_exact_limit(coordinator):
    """A response exactly at the configured ceiling is accepted."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return a body exactly at the decoded-content ceiling."""
        return httpx.Response(
            200,
            content=b"a" * MAX_RESPONSE_BYTES,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await BlueprintUpdateCoordinator._async_get_bounded_response(
            client, "https://example.com", {}
        )

    assert len(response.content) == MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_bounded_response_requests_identity_encoding():
    """Blueprint requests avoid compressed transfer encodings."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return plain YAML after verifying the explicit encoding preference."""
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["If-None-Match"] == '"test-etag"'
        return httpx.Response(200, content=b"blueprint: plain text", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await BlueprintUpdateCoordinator._async_get_bounded_response(
            client,
            "https://example.com/blueprint.yaml",
            {"If-None-Match": '"test-etag"', "Accept-Encoding": "gzip"},
        )

    assert response.content == b"blueprint: plain text"


@pytest.mark.asyncio
async def test_bounded_response_rejects_malformed_compression():
    """Malformed compression remains an error when a server ignores identity encoding."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return plain bytes incorrectly advertised as gzip encoded."""
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=b"blueprint: plain text",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.DecodingError):
            await BlueprintUpdateCoordinator._async_get_bounded_response(
                client, "https://example.com/blueprint.yaml", {}
            )


@pytest.mark.asyncio
async def test_async_fetch_content_retry_limit(coordinator):
    """Test that _async_fetch_content retries exactly MAX_RETRIES times."""
    mock_session = MagicMock(spec=httpx.AsyncClient)
    coordinator._async_get_bounded_response = AsyncMock(
        side_effect=httpx.RequestError("Fetch failed")
    )

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
        pytest.raises(httpx.RequestError, match="Fetch failed"),
    ):
        await coordinator._async_fetch_content(mock_session, "https://url")

    assert coordinator._async_get_bounded_response.call_count == MAX_RETRIES + 1


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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_response)

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
        await coordinator._async_fetch_content(mock_session, "https://example.com/url1")
        await coordinator._async_fetch_content(mock_session, "https://example.com/url2")

        mock_random.assert_called_with(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)

        expected_delay = (100.1 + MIN_SEND_INTERVAL) - 100.2
        mock_sleep.assert_called_with(pytest.approx(expected_delay))
        assert mock_sleep.call_count == 1

        # Query other host and verify it does not trigger pacing delay
        await coordinator._async_fetch_content(mock_session, "https://otherdomain.com/url3")
        assert mock_sleep.call_count == 1


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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_response)
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
        await coordinator._async_fetch_content(mock_session, "https://example.com/url1")
        await coordinator._async_fetch_content(mock_session, "https://example.com/url2")

        mock_random.assert_called_with(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)

        expected_delay = (200.1 + MAX_SEND_INTERVAL) - 200.2
        mock_sleep.assert_called_with(pytest.approx(expected_delay))
        assert mock_sleep.call_count == 1

        # Query other host and verify it does not trigger pacing delay
        await coordinator._async_fetch_content(mock_session, "https://otherdomain.com/url3")
        assert mock_sleep.call_count == 1


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
            patch.object(
                coordinator,
                "_async_get_bounded_response",
                new_callable=AsyncMock,
            ) as mock_get,
        ):
            mock_get.return_value = MagicMock(spec=httpx.Response)
            mock_get.return_value.status_code = HTTPStatus.OK
            mock_get.return_value.is_redirect = False
            mock_get.return_value.url = httpx.URL("https://example.com/path")
            mock_get.return_value.headers = {"ETag": "new", "Content-Type": "text/yaml"}
            mock_get.return_value.text = "blueprint:\n  name: Test"
            mock_get.return_value.raise_for_status = MagicMock()

            tasks = [
                coordinator._async_fetch_content(async_client, "https://example.com/url1/bp.yaml"),
                coordinator._async_fetch_content(async_client, "https://example.com/url2/bp.yaml"),
                coordinator._async_fetch_content(async_client, "https://example.com/url3/bp.yaml"),
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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_resp_redirect)

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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_resp_unsafe)

    with (
        patch.object(coordinator, "_is_safe_url", side_effect=[True, False]),
        pytest.raises(httpx.HTTPError, match="Security violation"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "https://example.com/start", {}
        )
    coordinator._async_get_bounded_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_with_redirect_guard_final_https(coordinator):
    """Test that the redirect guard enforces HTTPS for the final destination."""
    mock_session = MagicMock(spec=httpx.AsyncClient)

    mock_resp_final_safe = MagicMock()
    mock_resp_final_safe.status_code = HTTPStatus.OK
    mock_resp_final_safe.is_redirect = False
    mock_resp_final_safe.url = httpx.URL("https://safe.com/bp.yaml")
    mock_resp_final_safe.raise_for_status = MagicMock()

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_resp_final_safe)

    with patch.object(coordinator, "_is_safe_url", return_value=True):
        resp = await coordinator._execute_with_redirect_guard(
            mock_session, "https://safe.com/bp.yaml", {}
        )
        assert str(resp.url) == "https://safe.com/bp.yaml"

    mock_resp_final_unsafe = MagicMock()
    mock_resp_final_unsafe.status_code = HTTPStatus.OK
    mock_resp_final_unsafe.is_redirect = False
    mock_resp_final_unsafe.url = httpx.URL("http://unsafe.com/bp.yaml")
    mock_resp_final_unsafe.raise_for_status = MagicMock()

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_resp_final_unsafe)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=True),
        pytest.raises(httpx.HTTPError, match="Security violation"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "https://start.com/bp.yaml", {}
        )
    coordinator._async_get_bounded_response.assert_awaited_once()


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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_response)

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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_response)

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

    coordinator._async_get_bounded_response = AsyncMock(return_value=mock_response)

    with (
        patch.object(coordinator, "_is_safe_url", return_value=True),
        pytest.raises(httpx.HTTPError, match="Security violation"),
    ):
        await coordinator._execute_with_redirect_guard(
            mock_session, "https://example.com/bp.yaml", {}
        )
    coordinator._async_get_bounded_response.assert_awaited_once()


@pytest.mark.parametrize(
    (
        "provider_available",
        "content_type",
        "body",
        "url",
        "expected_match",
        "should_parse",
        "expected_json",
    ),
    [
        (
            False,
            "application/json",
            b"{}",
            "https://example.com/data",
            "Unsupported content type",
            False,
            None,
        ),
        (
            True,
            "application/json",
            b"{",
            "https://community.home-assistant.io/t/1.json",
            "Invalid JSON response",
            False,
            None,
        ),
        (
            True,
            "application/json",
            b'{"post_stream": {"posts": []}}',
            "https://community.home-assistant.io/t/1.json",
            "Failed to extract blueprint content from JSON",
            True,
            {"post_stream": {"posts": []}},
        ),
        (
            True,
            "text/html",
            b"<html></html>",
            "https://example.com/page",
            "Failed to extract blueprint content from response",
            True,
            None,
        ),
    ],
    ids=[
        "unsupported-unowned-content",
        "invalid-json",
        "empty-json-provider-content",
        "empty-non-json-provider-content",
    ],
)
@pytest.mark.asyncio
async def test_parse_provider_response_rejects_invalid_content(
    coordinator,
    provider_available,
    content_type,
    body,
    url,
    expected_match,
    should_parse,
    expected_json,
):
    """Verify invalid provider responses raise behavior-specific errors."""
    provider = MagicMock() if provider_available else None
    if provider is not None:
        provider.parse_content.return_value = None

    response = httpx.Response(
        200,
        headers={"Content-Type": content_type},
        content=body,
        request=httpx.Request("GET", url),
    )

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        pytest.raises(HomeAssistantError, match=expected_match),
    ):
        await coordinator._parse_provider_response(response, url)

    if provider is None:
        return

    if should_parse:
        provider.parse_content.assert_called_once_with(response.text, expected_json)
    else:
        provider.parse_content.assert_not_called()
