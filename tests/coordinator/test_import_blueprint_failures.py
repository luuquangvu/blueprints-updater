"""Tests for async_import_blueprint failure handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.const import BLUEPRINTS_DATA_DIR, SourceProviderType


def _provider(canonical_url: str = "https://example.com/bp.yaml") -> MagicMock:
    """Build a provider mock that owns a canonical blueprint URL."""
    provider = MagicMock()
    provider.provider_type = SourceProviderType.GITHUB
    provider.normalize_url.return_value = canonical_url
    provider.get_metadata.return_value = {"author": "author", "name": "name"}
    return provider


def _response(url: str = "https://example.com/bp.yaml") -> httpx.Response:
    """Build a successful YAML response for import tests."""
    return httpx.Response(
        200,
        content=b"blueprint:\n  name: Imported\n  domain: automation\n",
        headers={"Content-Type": "text/yaml"},
        request=httpx.Request("GET", url),
    )


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_unsupported_provider(coordinator):
    """Verify unsupported import sources fail before fetching."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=None,
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("not-a-url", confirm=True)

    assert err.value.translation_key == "unsupported_source"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_unsafe_canonical_url(coordinator):
    """Verify provider normalization is checked with the same URL safety guard."""
    provider = _provider("http://unsafe.example/bp.yaml")

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(coordinator, "_is_safe_url", AsyncMock(side_effect=[True, False])),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "unsafe_url"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_empty_provider_content(coordinator):
    """Verify empty parsed provider content is rejected before metadata is used."""
    provider = _provider()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(coordinator, "_parse_provider_response", AsyncMock(return_value="")),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "empty_content"
    provider.get_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_async_import_blueprint_wraps_fetch_errors(coordinator):
    """Verify transport errors are exposed as service fetch errors."""
    provider = _provider()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(
            coordinator,
            "_execute_with_redirect_guard",
            AsyncMock(side_effect=httpx.HTTPError("network down")),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "fetch_error"
    assert err.value.translation_placeholders == {"error": "network down"}


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_malformed_provider_metadata(coordinator):
    """Verify provider metadata must include author and name."""
    provider = _provider()
    provider.get_metadata.return_value = {"author": "author"}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value="blueprint:\n  name: Imported\n  domain: automation\n"),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "fetch_error"
    placeholders = err.value.translation_placeholders
    assert placeholders is not None
    assert "Malformed metadata" in placeholders["error"]


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_yaml_without_blueprint_block(coordinator):
    """Verify YAML that is syntactically valid but not a blueprint is rejected."""
    provider = _provider()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator, "_parse_provider_response", AsyncMock(return_value="not_blueprint: true")
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "invalid_yaml"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_existing_non_string_source_url(coordinator):
    """Verify import conflict detection rejects malformed stored source_url values."""
    provider = _provider()
    full_path = coordinator.hass.config.path(
        BLUEPRINTS_DATA_DIR,
        "automation/author/name.yaml",
    )
    coordinator.data[full_path] = {"source_url": 123}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.registry.get_provider",
            return_value=provider,
        ),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value="blueprint:\n  name: Imported\n  domain: automation\n"),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("https://example.com/bp.yaml", confirm=True)

    assert err.value.translation_key == "import_invalid_source_type"
    assert err.value.translation_placeholders == {"type": "int"}
