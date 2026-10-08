"""Tests for version-compatibility helpers."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from tests.compat import (
    async_call_service_compat,
    create_service_call,
    patch_service_call_compat,
)


def _admin_service_with_response(
    hass: Any,
    domain: str,
    service: str,
    service_func: Any,
    schema: Any = None,
    supports_response: Any = None,
) -> None:
    """Mock admin service registration signature supporting supports_response."""


def _admin_service_without_response(
    hass: Any,
    domain: str,
    service: str,
    service_func: Any,
    schema: Any = None,
) -> None:
    """Mock admin service registration signature omitting supports_response."""


@pytest.mark.asyncio
async def test_async_call_service_compat_with_response_supported() -> None:
    """Test async_call_service_compat passes return_response=True when supported."""
    hass = MagicMock(spec=HomeAssistant)
    hass.services = MagicMock()
    mock_async_call = AsyncMock(return_value={"success": True})
    hass.services.async_call = mock_async_call

    with patch(
        "homeassistant.helpers.service.async_register_admin_service",
        _admin_service_with_response,
    ):
        result = await async_call_service_compat(
            hass,
            "test_domain",
            "test_service",
            {"key": "value"},
            blocking=True,
            return_response=True,
        )

        mock_async_call.assert_awaited_once_with(
            "test_domain",
            "test_service",
            {"key": "value"},
            blocking=True,
            return_response=True,
        )
        assert result == {"success": True}


@pytest.mark.asyncio
async def test_async_call_service_compat_with_response_unsupported() -> None:
    """Test async_call_service_compat omits return_response when unsupported."""
    hass = MagicMock(spec=HomeAssistant)
    hass.services = MagicMock()
    mock_async_call = AsyncMock(return_value=None)
    hass.services.async_call = mock_async_call

    with patch(
        "homeassistant.helpers.service.async_register_admin_service",
        _admin_service_without_response,
    ):
        result = await async_call_service_compat(
            hass,
            "test_domain",
            "test_service",
            {"key": "value"},
            blocking=True,
            return_response=True,
        )

        mock_async_call.assert_awaited_once_with(
            "test_domain",
            "test_service",
            {"key": "value"},
            blocking=True,
        )
        assert result is None


@pytest.mark.asyncio
async def test_async_call_service_compat_without_return_response() -> None:
    """Test async_call_service_compat defaults service_data and respects blocking=False."""
    hass = MagicMock(spec=HomeAssistant)
    hass.services = MagicMock()
    mock_async_call = AsyncMock(return_value=None)
    hass.services.async_call = mock_async_call

    with patch(
        "homeassistant.helpers.service.async_register_admin_service",
        _admin_service_with_response,
    ):
        result = await async_call_service_compat(
            hass,
            "test_domain",
            "test_service",
            blocking=False,
            return_response=False,
        )

        mock_async_call.assert_awaited_once_with(
            "test_domain",
            "test_service",
            {},
            blocking=False,
        )
        assert result is None


def test_create_service_call() -> None:
    """Test create_service_call helper creates a valid ServiceCall."""
    hass = MagicMock(spec=HomeAssistant)
    call = create_service_call(hass, "test_domain", "test_service", {"foo": "bar"})
    assert call.domain == "test_domain"
    assert call.service == "test_service"
    assert call.data == {"foo": "bar"}

    call_empty = create_service_call(hass, "test_domain", "test_service")
    assert call_empty.data == {}


def test_patch_service_call_compat_idempotent() -> None:
    """Test patch_service_call_compat is safe and idempotent."""
    patch_service_call_compat()
    assert getattr(ServiceCall, "_compat_patched", False) is True
