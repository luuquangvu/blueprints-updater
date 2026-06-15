"""Tests for async_import_blueprint failure handling."""

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import httpx
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.const import BLUEPRINTS_DATA_DIR, SourceProviderType

PROVIDER_LOOKUP = "custom_components.blueprints_updater.coordinator.registry.get_provider"
IMPORT_URL = "https://example.com/bp.yaml"
CONFLICTING_SOURCE_URL = "https://other.example/bp.yaml"
IMPORTED_BLUEPRINT = "blueprint:\n  name: Imported\n  domain: automation\n"
EXISTING_BLUEPRINT = "blueprint:\n  name: Existing\n  domain: automation\n"
EXISTING_IMPORT_PATH = "/config/blueprints/automation/author/name.yaml"


def _provider(canonical_url: str = IMPORT_URL) -> MagicMock:
    """Build a provider mock that owns a canonical blueprint URL."""
    provider = MagicMock()
    provider.provider_type = SourceProviderType.GITHUB
    provider.normalize_url.return_value = canonical_url
    provider.get_metadata.return_value = {"author": "author", "name": "name"}
    return provider


def _patched_provider(provider):
    """Patch provider lookup for an import attempt."""
    return patch(PROVIDER_LOOKUP, return_value=provider)


def _response(url: str = IMPORT_URL) -> httpx.Response:
    """Build a successful YAML response for import tests."""
    return httpx.Response(
        200,
        content=IMPORTED_BLUEPRINT.encode("utf-8"),
        headers={"Content-Type": "text/yaml"},
        request=httpx.Request("GET", url),
    )


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_unsupported_provider(coordinator):
    """Verify unsupported import sources fail before fetching."""
    with (
        _patched_provider(None),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint("not-a-url", confirm=True)

    assert err.value.translation_key == "unsupported_source"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_unsafe_canonical_url(coordinator):
    """Verify provider normalization is checked with the same URL safety guard."""
    provider = _provider("http://unsafe.example/bp.yaml")

    with (
        _patched_provider(provider),
        patch.object(coordinator, "_is_safe_url", AsyncMock(side_effect=[True, False])),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "unsafe_blueprint_url"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_empty_provider_content(coordinator):
    """Verify empty parsed provider content is rejected before metadata is used."""
    provider = _provider()

    with (
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(coordinator, "_parse_provider_response", AsyncMock(return_value="")),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "empty_blueprint_content"
    provider.get_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_async_import_blueprint_wraps_fetch_errors(coordinator):
    """Verify transport errors are exposed as service fetch errors."""
    provider = _provider()

    with (
        _patched_provider(provider),
        patch.object(
            coordinator,
            "_execute_with_redirect_guard",
            AsyncMock(side_effect=httpx.HTTPError("network down")),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "fetch_blueprint_error"
    assert err.value.translation_placeholders == {"error": "network down"}


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_malformed_provider_metadata(coordinator):
    """Verify provider metadata must include author and name."""
    provider = _provider()
    provider.get_metadata.return_value = {"author": "author"}

    with (
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "fetch_blueprint_error"
    placeholders = err.value.translation_placeholders
    assert placeholders is not None
    assert "Malformed metadata" in placeholders["error"]


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_yaml_without_blueprint_block(coordinator):
    """Verify YAML that is syntactically valid but not a blueprint is rejected."""
    provider = _provider()

    with (
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator, "_parse_provider_response", AsyncMock(return_value="not_blueprint: true")
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

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
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "import_invalid_source_type"
    assert err.value.translation_placeholders == {"type": "int"}


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_unsafe_destination_path(coordinator):
    """Verify imported metadata cannot write outside the safe blueprint root."""
    provider = _provider()

    with (
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        patch.object(coordinator, "_is_safe_path", return_value=False),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "unsafe_blueprint_path"


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_existing_data_with_conflicting_source(
    coordinator, tmp_path
):
    """Verify existing coordinator data with a different source_url blocks import."""
    provider = _provider()
    provider.normalize_url.side_effect = lambda url: IMPORT_URL if url == IMPORT_URL else url
    existing_path = tmp_path / "name.yaml"
    coordinator.data[str(existing_path)] = {"source_url": CONFLICTING_SOURCE_URL}

    with (
        _patched_provider(provider),
        patch.object(coordinator.hass.config, "path", return_value=str(existing_path)),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "import_path_conflict"
    placeholders = err.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["existing_url"] == CONFLICTING_SOURCE_URL


@pytest.mark.asyncio
async def test_async_import_blueprint_rejects_existing_file_without_source_url(coordinator):
    """Verify an existing destination without source_url is treated as a conflict."""
    provider = _provider()
    existing_path = EXISTING_IMPORT_PATH

    with (
        _patched_provider(provider),
        patch.object(coordinator.hass.config, "path", return_value=existing_path),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch(
            "builtins.open",
            mock_open(read_data=EXISTING_BLUEPRINT),
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "import_path_conflict"


@pytest.mark.asyncio
async def test_async_import_blueprint_surfaces_blueprint_validation_error(coordinator):
    """Verify schema validation errors keep their translation key and detail."""
    provider = _provider()

    with (
        _patched_provider(provider),
        patch.object(
            coordinator, "_execute_with_redirect_guard", AsyncMock(return_value=_response())
        ),
        patch.object(
            coordinator,
            "_parse_provider_response",
            AsyncMock(return_value=IMPORTED_BLUEPRINT),
        ),
        patch.object(
            coordinator, "_validate_blueprint", return_value="blueprint_validation_error|bad schema"
        ),
        pytest.raises(ServiceValidationError) as err,
    ):
        await coordinator.async_import_blueprint(IMPORT_URL, confirm=True)

    assert err.value.translation_key == "blueprint_validation_error"
    assert err.value.translation_placeholders == {"error": "bad schema"}
