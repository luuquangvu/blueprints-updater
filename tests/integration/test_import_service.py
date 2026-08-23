"""Tests for the import_blueprint service."""

import socket
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import (
    DOMAIN,
    IntegrationService,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
async def setup_integration(hass: HomeAssistant) -> AsyncIterator[None]:
    """Set up the integration for tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_entry",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
        ),
        patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _assert_imported_blueprint(
    hass: HomeAssistant,
    relative_path: str,
    source_url: str,
    etag: str,
) -> None:
    """Assert a service import reached disk, coordinator state, and entity registry."""
    path = Path(hass.config.path("blueprints")) / relative_path
    assert path.is_file()
    assert f"source_url: {source_url}" in path.read_text(encoding="utf-8")

    coordinator = hass.data[DOMAIN]["coordinators"]["test_entry"]
    info = coordinator.data[str(path)]
    assert info["source_url"] == source_url
    assert info["etag"] == etag
    assert info["updatable"] is False

    unique_id = BlueprintUpdateCoordinator.generate_unique_id("test_entry", relative_path)
    entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, unique_id)
    assert entity_id is not None


@pytest.mark.asyncio
async def test_import_blueprint_no_confirm(hass, setup_integration):
    """Test import_blueprint service without confirmation."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": "https://example.com/bp.yaml", "confirm": False},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_blueprint_unsafe_url(hass, setup_integration):
    """Test import_blueprint service with unsafe (local) URL."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": "http://192.168.1.1/bp.yaml", "confirm": True},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_blueprint_unsupported_source(hass, setup_integration):
    """Test import_blueprint service with unsupported source."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": "not-a-url", "confirm": True},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_blueprint_success_github(hass, setup_integration, respx_mock):
    """Test successful blueprint import from GitHub."""
    url = "https://github.com/user/repo/blob/main/test.yaml"
    raw_url = "https://raw.githubusercontent.com/user/repo/main/test.yaml"
    content = "blueprint:\n  name: Imported\n  domain: automation\n"

    respx_mock.get(raw_url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK,
            content=content,
            headers={
                "Content-Type": "text/yaml",
                "ETag": '"abc"',
                "Last-Modified": "Wed, 13 May 2026 01:00:00 GMT",
            },
        )
    )

    await hass.services.async_call(
        DOMAIN,
        IntegrationService.IMPORT_BLUEPRINT,
        {"url": url, "confirm": True},
        blocking=True,
    )
    await hass.async_block_till_done()

    _assert_imported_blueprint(
        hass,
        "automation/user/test.yaml",
        raw_url,
        '"abc"',
    )


@pytest.mark.asyncio
async def test_import_blueprint_invalid_yaml(hass, setup_integration, respx_mock):
    """Test import_blueprint with invalid YAML."""
    url = "https://github.com/user/repo/blob/main/test.yaml"
    raw_url = "https://raw.githubusercontent.com/user/repo/main/test.yaml"
    content = "invalid: yaml: :"

    respx_mock.get(raw_url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK,
            content=content,
            headers={
                "Content-Type": "text/yaml",
                "ETag": '"abc"',
                "Last-Modified": "Wed, 13 May 2026 01:00:00 GMT",
            },
        )
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": url, "confirm": True},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_blueprint_invalid_content_type(hass, setup_integration, respx_mock):
    """Test import_blueprint with invalid content type for generic provider."""
    url = "https://example.com/page.html"

    respx_mock.get(url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK, content="<html></html>", headers={"Content-Type": "text/html"}
        )
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": url, "confirm": True},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_blueprint_success_generic(hass, setup_integration, respx_mock):
    """Test successful blueprint import from a generic YAML URL."""
    url = "https://pastebin.com/raw/xxxx"
    content = "blueprint:\n  name: Generic Blueprint\n  domain: automation\n"

    respx_mock.get(url).mock(
        return_value=httpx.Response(
            HTTPStatus.OK,
            content=content,
            headers={
                "Content-Type": "text/plain",
                "ETag": '"xyz"',
            },
        )
    )

    await hass.services.async_call(
        DOMAIN,
        IntegrationService.IMPORT_BLUEPRINT,
        {"url": url, "confirm": True},
        blocking=True,
    )
    await hass.async_block_till_done()

    _assert_imported_blueprint(
        hass,
        "automation/pastebin.com/generic_blueprint.yaml",
        url,
        '"xyz"',
    )
