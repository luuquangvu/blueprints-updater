"""Compatibility imports for libraries provided by Home Assistant."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING


def _import_first(*module_names: str) -> ModuleType:
    """Return the first importable module from a compatibility list."""
    last_error: ModuleNotFoundError | None = None
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as err:
            if err.name != module_name:
                raise
            last_error = err
    raise ModuleNotFoundError(
        f"None of the supported modules are installed: {', '.join(module_names)}"
    ) from last_error


def _http_library_pair(module_name: str) -> tuple[ModuleType, ModuleType]:
    """Import an HTTP client and the transport library it is paired with."""
    suffix = module_name.removeprefix("httpx") if module_name.startswith("httpx") else ""
    httpcore_name = f"httpcore{suffix}" if suffix else "httpcore"
    return importlib.import_module(module_name), importlib.import_module(httpcore_name)


def _active_http_libraries() -> tuple[ModuleType, ModuleType]:
    """Return the HTTP client and transport modules used by Home Assistant."""
    httpx_client = importlib.import_module("homeassistant.helpers.httpx_client")

    client_class = getattr(httpx_client, "HassHttpXAsyncClient", None)
    if isinstance(client_class, type):
        for base in client_class.__mro__[1:]:
            if base.__name__ == "AsyncClient":
                return _http_library_pair(base.__module__.partition(".")[0])

    for module_name in ("httpx2", "httpx"):
        if isinstance(getattr(httpx_client, module_name, None), ModuleType):
            return _http_library_pair(module_name)

    return _http_library_pair(_import_first("httpx2", "httpx").__name__)


if TYPE_CHECKING:
    import httpcore2 as httpcore
    import httpx2 as httpx
    import probatio as vol
else:
    httpx, httpcore = _active_http_libraries()
    vol = _import_first("probatio", "voluptuous")

__all__ = ["httpcore", "httpx", "vol"]
