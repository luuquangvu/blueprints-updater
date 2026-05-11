"""Fixtures for Blueprints Updater tests."""

import asyncio
import ipaddress
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.const import SPECIAL_USE_TLDS
from custom_components.blueprints_updater.utils import is_ip_safe


@pytest.fixture(autouse=True)
def mock_asyncio_sleep():
    """Mock asyncio.sleep for all tests to run instantly."""
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.return_value = None
        yield


@pytest.fixture(autouse=True)
def mock_storage():
    """Mock Home Assistant storage."""
    with patch("custom_components.blueprints_updater.coordinator.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock(return_value=None)
        yield mock_store


@pytest.fixture
def mock_makedirs():
    """Mock os.makedirs for all tests."""
    with patch("custom_components.blueprints_updater.coordinator.os.makedirs") as mock:
        yield mock


@pytest.fixture
def _mock_hass():
    """Mock HomeAssistant fixture for unit tests."""
    hass_mock = MagicMock(spec=HomeAssistant)
    hass_mock.config = MagicMock()
    hass_mock.config.path.return_value = "/config/blueprints"
    hass_mock.services = MagicMock()
    hass_mock.services.async_call = AsyncMock(return_value=None)

    hass_mock.bus = MagicMock()
    hass_mock.bus.async_listen = MagicMock(return_value=lambda: None)

    async def async_add_executor_job(target, *args, **kwargs):
        """Mock running sync jobs in an executor."""
        return target(*args, **kwargs)

    hass_mock.async_add_executor_job = AsyncMock(side_effect=async_add_executor_job)

    def async_create_background_task(coro, name=None):
        """Mock creating a background task."""
        return asyncio.create_task(coro, name=name)

    hass_mock.async_create_background_task = MagicMock(side_effect=async_create_background_task)

    hass_mock.data = {}
    return hass_mock


@pytest.fixture(autouse=True)
def mock_getaddrinfo(request, monkeypatch):
    """Mock getaddrinfo to block external network access.

    Delegates localhost, 127.0.0.1, and ::1 to the real resolver.
    For other special-use domains (e.g., .local, .home.arpa), returns
    127.0.0.2 to avoid HA's network security filters without triggering
    actual DNS resolution. All other hosts resolve to 1.1.1.1 (a dummy
    external IP) to prevent tests from touching the real network.

    Can be bypassed using @pytest.mark.real_network.
    """
    if "real_network" in request.keywords:
        return

    real_getaddrinfo = socket.getaddrinfo

    def _fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host is None:
            return real_getaddrinfo(host, port, family, type, proto, flags)

        if isinstance(host, bytes):
            host = host.decode("utf-8")
        hostname = host.rstrip(".").lower()
        is_local = False
        try:
            ip = ipaddress.ip_address(hostname)
            is_local = not is_ip_safe(ip)
        except ValueError:
            for tld in SPECIAL_USE_TLDS:
                if hostname == tld or hostname.endswith("." + tld):
                    is_local = True
                    break

        if is_local and hostname in ("localhost", "127.0.0.1", "::1"):
            return real_getaddrinfo(host, port, family, type, proto, flags)

        results = []
        families = [socket.AF_INET, socket.AF_INET6] if family == socket.AF_UNSPEC else [family]

        for f in families:
            if f == socket.AF_INET:
                dummy_ip = "127.0.0.2" if is_local else "1.1.1.1"
                addr_tuple = (dummy_ip, port)
            elif f == socket.AF_INET6:
                dummy_ip = "::2" if is_local else "2606:4700:4700::1111"
                addr_tuple = (dummy_ip, port, 0, 0)
            else:
                msg = f"Address family {f} not supported in tests"
                raise socket.gaierror(socket.EAI_FAMILY, msg)

            results.append(
                (
                    f,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    addr_tuple,
                )
            )

        return results

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    return
