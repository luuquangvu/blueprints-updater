"""Fixtures for Blueprints Updater tests."""

import asyncio
import ipaddress
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.const import SPECIAL_USE_TLDS
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


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
def mock_getaddrinfo():
    """Mock socket.getaddrinfo to avoid DNS resolution blocking in tests.

    This returns a safe dummy IP for all non-local hostnames, preventing
    the 'DNS resolution disabled in tests' error while allowing the
    integration's safety checks to proceed.
    """
    original_getaddrinfo = socket.getaddrinfo

    def side_effect(host, port, family=0, type=0, proto=0, flags=0):
        is_local = False
        try:
            ip = ipaddress.ip_address(host)
            is_local = not BlueprintUpdateCoordinator._is_ip_safe(ip)
        except ValueError:
            hostname_lower = host.lower()
            if hostname_lower in SPECIAL_USE_TLDS:
                is_local = True
            else:
                parts = hostname_lower.rsplit(".", 1)
                if len(parts) > 1 and parts[-1] in SPECIAL_USE_TLDS:
                    is_local = True

        if is_local:
            return original_getaddrinfo(host, port, family, type, proto, flags)

        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]

    with patch("socket.getaddrinfo", side_effect=side_effect) as mock_get:
        yield mock_get
