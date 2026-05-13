"""Tests for the import_blueprint service."""

import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import DOMAIN, IntegrationService


@pytest.fixture
async def setup_integration(hass):
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
            200,
            content=content,
            headers={
                "Content-Type": "text/yaml",
                "ETag": '"abc"',
                "Last-Modified": "Wed, 13 May 2026 01:00:00 GMT",
            },
        )
    )

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator.async_install_blueprint",
        new_callable=AsyncMock,
    ) as mock_install:
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": url, "confirm": True},
            blocking=True,
        )

        mock_install.assert_awaited_once()
        assert mock_install.await_args is not None
        args, kwargs = mock_install.await_args
        assert "automation/user/test.yaml" in args[0]
        assert args[1] == content
        assert kwargs["source_url"] == raw_url
        assert kwargs["etag"] == '"abc"'
        assert kwargs["last_modified"] == "Wed, 13 May 2026 01:00:00 GMT"


@pytest.mark.asyncio
async def test_import_blueprint_invalid_yaml(hass, setup_integration, respx_mock):
    """Test import_blueprint with invalid YAML."""
    url = "https://github.com/user/repo/blob/main/test.yaml"
    raw_url = "https://raw.githubusercontent.com/user/repo/main/test.yaml"
    content = "invalid: yaml: :"

    respx_mock.get(raw_url).mock(
        return_value=httpx.Response(
            200,
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
            200, content="<html></html>", headers={"Content-Type": "text/html"}
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
            200,
            content=content,
            headers={
                "Content-Type": "text/plain",
                "ETag": '"xyz"',
            },
        )
    )

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator.async_install_blueprint",
        new_callable=AsyncMock,
    ) as mock_install:
        await hass.services.async_call(
            DOMAIN,
            IntegrationService.IMPORT_BLUEPRINT,
            {"url": url, "confirm": True},
            blocking=True,
        )

        mock_install.assert_awaited_once()
        assert mock_install.await_args is not None
        args, kwargs = mock_install.await_args
        assert "automation/pastebin.com/generic_blueprint.yaml" in args[0]
        assert args[1] == content
        assert kwargs["source_url"] == url
        assert kwargs["etag"] == '"xyz"'
        assert kwargs.get("last_modified") is None
