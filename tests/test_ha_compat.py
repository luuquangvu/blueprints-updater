"""Tests for Home Assistant compatibility imports."""

from types import ModuleType

import pytest

from custom_components.blueprints_updater import ha_compat


def test_active_http_libraries_follow_legacy_ha_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use legacy httpx/httpcore even when a newer client is installed."""
    httpx_module = ModuleType("httpx")
    httpx2_module = ModuleType("httpx2")
    httpcore_module = ModuleType("httpcore")
    httpcore2_module = ModuleType("httpcore2")
    factory_module = ModuleType("homeassistant.helpers.httpx_client")
    factory_module.__dict__["httpx"] = httpx_module

    installed_modules = {
        "homeassistant.helpers.httpx_client": factory_module,
        "httpx": httpx_module,
        "httpx2": httpx2_module,
        "httpcore": httpcore_module,
        "httpcore2": httpcore2_module,
    }
    monkeypatch.setattr(
        ha_compat.importlib,
        "import_module",
        lambda module_name: installed_modules[module_name],
    )

    assert ha_compat._active_http_libraries() == (httpx_module, httpcore_module)


def test_active_http_libraries_follow_new_ha_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use httpx2/httpcore2 when the Home Assistant factory imports them."""
    httpx2_module = ModuleType("httpx2")
    httpcore2_module = ModuleType("httpcore2")
    factory_module = ModuleType("homeassistant.helpers.httpx_client")
    factory_module.__dict__["httpx2"] = httpx2_module

    installed_modules = {
        "homeassistant.helpers.httpx_client": factory_module,
        "httpx2": httpx2_module,
        "httpcore2": httpcore2_module,
    }
    monkeypatch.setattr(
        ha_compat.importlib,
        "import_module",
        lambda module_name: installed_modules[module_name],
    )

    assert ha_compat._active_http_libraries() == (httpx2_module, httpcore2_module)


def test_active_http_libraries_follow_factory_client_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer the client base class used by the Home Assistant factory."""
    httpx_module = ModuleType("httpx")
    httpx2_module = ModuleType("httpx2")
    httpcore_module = ModuleType("httpcore")
    httpcore2_module = ModuleType("httpcore2")
    async_client_base = type("AsyncClient", (), {"__module__": "httpx2"})
    factory_client = type("HassHttpXAsyncClient", (async_client_base,), {})
    factory_module = ModuleType("homeassistant.helpers.httpx_client")
    factory_module.__dict__["HassHttpXAsyncClient"] = factory_client
    factory_module.__dict__["httpx"] = httpx_module

    installed_modules = {
        "homeassistant.helpers.httpx_client": factory_module,
        "httpx": httpx_module,
        "httpx2": httpx2_module,
        "httpcore": httpcore_module,
        "httpcore2": httpcore2_module,
    }
    monkeypatch.setattr(
        ha_compat.importlib,
        "import_module",
        lambda module_name: installed_modules[module_name],
    )

    assert ha_compat._active_http_libraries() == (httpx2_module, httpcore2_module)


def test_active_http_libraries_fall_back_to_importable_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to the first importable HTTP client when the factory is opaque."""
    httpx2_module = ModuleType("httpx2")
    httpcore2_module = ModuleType("httpcore2")
    factory_module = ModuleType("homeassistant.helpers.httpx_client")
    factory_client = type("HassHttpXAsyncClient", (), {})
    factory_module.__dict__["HassHttpXAsyncClient"] = factory_client

    installed_modules = {
        "homeassistant.helpers.httpx_client": factory_module,
        "httpx2": httpx2_module,
        "httpcore2": httpcore2_module,
    }
    monkeypatch.setattr(
        ha_compat.importlib,
        "import_module",
        lambda module_name: installed_modules[module_name],
    )

    assert ha_compat._active_http_libraries() == (httpx2_module, httpcore2_module)


def test_import_first_falls_back_to_next_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the next supported module when the preferred one is not installed."""
    voluptuous_module = ModuleType("voluptuous")

    def fake_import(module_name: str) -> ModuleType:
        if module_name == "probatio":
            raise ModuleNotFoundError("not installed", name=module_name)
        return voluptuous_module

    monkeypatch.setattr(ha_compat.importlib, "import_module", fake_import)

    assert ha_compat._import_first("probatio", "voluptuous") is voluptuous_module


def test_import_first_reraises_missing_internal_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not mask a broken preferred module by silently using a fallback."""

    def fake_import(module_name: str) -> ModuleType:
        raise ModuleNotFoundError("missing dependency", name="dependency")

    monkeypatch.setattr(ha_compat.importlib, "import_module", fake_import)

    with pytest.raises(ModuleNotFoundError, match="missing dependency"):
        ha_compat._import_first("probatio", "voluptuous")


def test_import_first_reports_when_no_module_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report a clear error when none of the supported modules is installed."""

    def fake_import(module_name: str) -> ModuleType:
        raise ModuleNotFoundError("not installed", name=module_name)

    monkeypatch.setattr(ha_compat.importlib, "import_module", fake_import)

    with pytest.raises(ModuleNotFoundError, match="None of the supported modules"):
        ha_compat._import_first("probatio", "voluptuous")
