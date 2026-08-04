"""Connection-bound network safety for Blueprints Updater."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator

import httpcore
import httpx
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.util import ssl as ha_ssl

from .const import DOMAIN, REQUEST_TIMEOUT, SPECIAL_USE_TLDS
from .utils import is_ip_safe

SSL_ALPN_HTTP11: tuple[str, ...] = getattr(
    ha_ssl,
    "SSL_ALPN_HTTP11",
    ("http/1.1",),
)
SSL_ALPN_HTTP11_HTTP2: tuple[str, ...] = getattr(
    ha_ssl,
    "SSL_ALPN_HTTP11_HTTP2",
    ("http/1.1", "h2"),
)
_HAS_NATIVE_ALPN_SUPPORT = hasattr(ha_ssl, "SSL_ALPN_HTTP11")
_IDNA_URL_TEMPLATE = httpx.URL("https://placeholder.invalid/")
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE_CONNECTIONS = 20
_KEEPALIVE_EXPIRY = 5.0
_MIN_CONNECT_ATTEMPT_TIMEOUT = 0.5


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Map an httpcore error to the nearest public httpx transport error."""
    try:
        yield
    except Exception as err:
        for source_type in type(err).__mro__:
            if not source_type.__module__.startswith("httpcore"):
                continue
            target_type = getattr(httpx, source_type.__name__, None)
            if isinstance(target_type, type) and issubclass(target_type, httpx.TransportError):
                raise target_type(str(err)) from err
        raise


def _client_ssl_context(alpn_protocols: tuple[str, ...]) -> ssl.SSLContext:
    """Return an SSL context using the ALPN API when Home Assistant provides it."""
    if _HAS_NATIVE_ALPN_SUPPORT:
        return ha_ssl.client_context(alpn_protocols=alpn_protocols)
    return ha_ssl.client_context()


def normalize_hostname(hostname: str) -> str | None:
    """Return a canonical IDNA hostname containing only valid DNS LDH labels.

    Unicode labels are converted to ASCII A-labels before enforcing hostname
    length and letter-digit-hyphen rules. DNS service labels containing
    underscores are intentionally rejected because this function validates
    URL hostnames, not SRV-style record names.
    """
    candidate = hostname[:-1] if hostname.endswith(".") else hostname
    if not candidate or candidate.endswith("."):
        return None

    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(candidate))

    try:
        normalized = _IDNA_URL_TEMPLATE.copy_with(host=candidate).raw_host.decode("ascii").lower()
    except (UnicodeError, httpx.InvalidURL):
        return None

    labels = normalized.split(".")
    if len(normalized) > 253 or any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not character.isalnum() and character != "-" for character in label)
        for label in labels
    ):
        return None
    return normalized


def is_special_use_hostname(hostname: str) -> bool:
    """Return whether a normalized hostname has a special-use DNS suffix."""
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in SPECIAL_USE_TLDS)


async def async_resolve_public_addresses(
    hass: HomeAssistant, hostname: str, port: int
) -> tuple[str, ...]:
    """Resolve a host and return only when every address is globally routable."""
    normalized_host = normalize_hostname(hostname)
    if normalized_host is None or is_special_use_hostname(normalized_host):
        return ()

    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(normalized_host)
        return (str(address),) if is_ip_safe(address) else ()

    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            address_info = await hass.async_add_executor_job(
                socket.getaddrinfo,
                normalized_host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
    except (TimeoutError, socket.gaierror):
        return ()

    addresses: list[str] = []
    for _family, _type, _protocol, _canonical_name, socket_address in address_info:
        try:
            address = ipaddress.ip_address(socket_address[0])
        except ValueError:
            continue
        if not is_ip_safe(address):
            return ()
        normalized_address = str(address)
        if normalized_address not in addresses:
            addresses.append(normalized_address)
    return tuple(addresses)


class SafeAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and pin every TCP connection to a validated public address."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the guarded backend."""
        self._hass = hass
        backend = httpcore.AnyIOBackend()
        if not isinstance(backend, httpcore.AsyncNetworkBackend):
            raise TypeError("Backend must be an AsyncNetworkBackend")
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect only to an address returned by the guarded resolver."""
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses = await async_resolve_public_addresses(self._hass, host, port)
        if not addresses:
            raise httpcore.ConnectError(f"Unsafe or unresolvable destination: {host}")

        last_error: Exception | None = None
        for index, address in enumerate(addresses):
            remaining_timeout = None if deadline is None else deadline - time.monotonic()
            if remaining_timeout is not None and remaining_timeout <= 0:
                break
            remaining_addresses = len(addresses) - index
            attempt_timeout = (
                None
                if remaining_timeout is None
                else min(
                    remaining_timeout,
                    max(
                        _MIN_CONNECT_ATTEMPT_TIMEOUT,
                        remaining_timeout / remaining_addresses,
                    ),
                )
            )
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as err:
                last_error = err

        raise httpcore.ConnectError(f"Unable to connect to validated destination: {host}") from (
            last_error
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Reject Unix-socket connections from remote blueprint fetches."""
        raise httpcore.ConnectError("Unix sockets are not valid blueprint destinations")

    async def sleep(self, seconds: float) -> None:
        """Delegate backend sleeps."""
        await self._backend.sleep(seconds)


class GuardedAsyncResponseStream(httpx.AsyncByteStream):
    """Adapt an httpcore response stream to httpx's public stream API."""

    def __init__(self, stream: AsyncIterable[object]) -> None:
        """Initialize the response stream adapter."""
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield response chunks while preserving httpx exception types."""
        with _map_httpcore_exceptions():
            async for chunk in self._stream:
                if not isinstance(chunk, bytes):
                    raise TypeError(f"Expected bytes chunk from stream, got {type(chunk).__name__}")
                yield chunk

    async def aclose(self) -> None:
        """Close the underlying httpcore stream when supported."""
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            await close()


class GuardedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """HTTPX transport whose socket connections use the guarded resolver."""

    def __init__(self, hass: HomeAssistant, *, http2: bool) -> None:
        """Initialize a Home Assistant-compatible guarded transport."""
        alpn_protocols = SSL_ALPN_HTTP11_HTTP2 if http2 else SSL_ALPN_HTTP11
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_client_ssl_context(alpn_protocols),
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
            http1=True,
            http2=http2,
            network_backend=SafeAsyncNetworkBackend(hass),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send an httpx request through the explicitly guarded pool."""
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("Guarded transport requires an asynchronous request stream")

        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            core_response = await self._pool.handle_async_request(core_request)

        if not isinstance(core_response.stream, AsyncIterable):
            raise TypeError("Guarded transport requires an asynchronous response stream")
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=GuardedAsyncResponseStream(core_response.stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        """Close the guarded connection pool."""
        await self._pool.aclose()


def get_guarded_async_client(
    hass: HomeAssistant,
    *,
    alpn_protocols: tuple[str, ...] | None = SSL_ALPN_HTTP11,
) -> httpx.AsyncClient:
    """Return a guarded client using one of the two supported ALPN modes.

    Only the exact ``SSL_ALPN_HTTP11_HTTP2`` tuple enables HTTP/2. ``None``
    and unsupported custom tuples are normalized to ``SSL_ALPN_HTTP11``.
    """
    effective_alpn = (
        SSL_ALPN_HTTP11_HTTP2 if alpn_protocols == SSL_ALPN_HTTP11_HTTP2 else SSL_ALPN_HTTP11
    )
    use_http2 = effective_alpn == SSL_ALPN_HTTP11_HTTP2
    clients = hass.data.setdefault(DOMAIN, {}).setdefault("guarded_http_clients", {})
    if client := clients.get(use_http2):
        return client

    transport = GuardedAsyncHTTPTransport(hass, http2=use_http2)
    if _HAS_NATIVE_ALPN_SUPPORT:
        client = create_async_httpx_client(
            hass,
            alpn_protocols=effective_alpn,
            http2=use_http2,
            transport=transport,
        )
    else:
        client = create_async_httpx_client(
            hass,
            http2=use_http2,
            transport=transport,
        )
    clients[use_http2] = client
    return client
