"""Compatibility utilities for Home Assistant version-aware testing."""

import inspect
from typing import Any, cast

from homeassistant.core import HomeAssistant, ServiceCall


def patch_service_call_compat():
    """Monkeypatch ServiceCall.__init__ if needed for backward compatibility."""
    if getattr(ServiceCall, "_compat_patched", False):
        return

    _original_init = ServiceCall.__init__
    try:
        sig = inspect.signature(_original_init)
    except (ValueError, TypeError, AttributeError):
        cast(Any, ServiceCall)._compat_patched = True
        return

    if "hass" not in sig.parameters:

        def _compat_init(self, *args, **kwargs):
            if args and not isinstance(args[0], (str, type(None))):
                return _original_init(self, *args[1:], **kwargs)
            return _original_init(self, *args, **kwargs)

        cast(Any, ServiceCall).__init__ = _compat_init

    cast(Any, ServiceCall)._compat_patched = True


def create_service_call(
    hass: HomeAssistant,
    domain: str,
    service: str,
    data: dict[str, Any] | None = None,
) -> ServiceCall:
    """Version-aware ServiceCall instantiation.

    Relies on ServiceCall monkeypatch in conftest.py for backward compatibility.
    """
    return ServiceCall(hass, domain, service, data or {})


async def async_call_service_compat(
    hass: HomeAssistant,
    domain: str,
    service: str,
    service_data: dict[str, Any] | None = None,
    *,
    blocking: bool = True,
    return_response: bool = False,
) -> Any:
    """Call a Home Assistant service with version-aware response handling."""
    from homeassistant.helpers.service import async_register_admin_service

    admin_svc_sig = inspect.signature(async_register_admin_service)
    supports_response_available = "supports_response" in admin_svc_sig.parameters

    if return_response and supports_response_available:
        return await hass.services.async_call(
            domain,
            service,
            service_data or {},
            blocking=blocking,
            return_response=True,
        )
    return await hass.services.async_call(
        domain,
        service,
        service_data or {},
        blocking=blocking,
    )
