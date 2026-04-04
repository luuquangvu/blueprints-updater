"""Tests for Blueprints Updater translations."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.update import BlueprintUpdateEntity


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord._translations = {}
        coord.setup_complete = True
        return coord


@pytest.mark.asyncio
async def test_coordinator_translate_flat(hass, coordinator):
    """Test translating a flat key (e.g., in 'common' category)."""
    hass.config.language = "en"

    translations = {f"component.{DOMAIN}.common.test_key": "Translated Value"}

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=translations,
    ) as mock_get:
        result = await coordinator.async_translate("test_key", category="common")
        assert result == "Translated Value"
        mock_get.assert_called_once_with(hass, "en", "common", [DOMAIN])


@pytest.mark.asyncio
async def test_coordinator_translate_nested(hass, coordinator):
    """Test translating a nested key with .message suffix (common in 'exceptions')."""
    hass.config.language = "vi"

    translations = {f"component.{DOMAIN}.exceptions.error_key.message": "Lỗi nội bộ"}

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=translations,
    ):
        result = await coordinator.async_translate("error_key", category="exceptions")
        assert result == "Lỗi nội bộ"


@pytest.mark.asyncio
async def test_coordinator_translate_cache_and_language_switch(hass, coordinator):
    """Test that changing language clears cache and loads new translations."""
    hass.config.language = "en"
    en_translations = {f"component.{DOMAIN}.common.hello": "Hello"}

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=en_translations,
    ) as mock_get:
        assert await coordinator.async_translate("hello") == "Hello"
        assert mock_get.call_count == 1

        assert await coordinator.async_translate("hello") == "Hello"
        assert mock_get.call_count == 1

    hass.config.language = "vi"
    vi_translations = {f"component.{DOMAIN}.common.hello": "Xin chào"}

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=vi_translations,
    ) as mock_get_vi:
        assert await coordinator.async_translate("hello") == "Xin chào"
        assert mock_get_vi.call_count == 1


@pytest.mark.asyncio
async def test_entity_localized_error(hass, coordinator):
    """Test that BlueprintUpdateEntity correctly localizes its last_error attribute."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
            "updatable": True,
            "last_error": "yaml_syntax_error|Line 5",
            "local_hash": "old",
        }
    }

    entity = BlueprintUpdateEntity(coordinator, path, coordinator.data[path])
    entity.hass = hass
    entity.entity_id = "update.test"

    translations = {
        f"component.{DOMAIN}.common.yaml_syntax_error": "Lỗi cú pháp: {error}",
        f"component.{DOMAIN}.common.update_available_short": "Có bản cập nhật",
    }

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=translations,
    ):
        assert entity.extra_state_attributes == {"last_error": "yaml_syntax_error|Line 5"}

        with patch.object(entity, "async_write_ha_state"):
            await entity._async_localize_strings()
        assert entity.extra_state_attributes == {"last_error": "Lỗi cú pháp: Line 5"}
        assert entity.release_summary == "Có bản cập nhật"


@pytest.mark.asyncio
async def test_coordinator_translate_formatting(hass, coordinator):
    """Test that formatting is applied correctly or safely ignored on failure."""
    hass.config.language = "en"
    translations = {f"component.{DOMAIN}.common.greet": "Hello {name}!"}

    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations",
        return_value=translations,
    ):
        assert await coordinator.async_translate("greet", name="World") == "Hello World!"
        assert await coordinator.async_translate("greet") == "Hello {name}!"


@pytest.mark.asyncio
async def test_coordinator_translate_not_ready(hass, coordinator):
    """Test that async_translate returns the key when setup_complete is False."""
    coordinator.setup_complete = False
    with patch(
        "custom_components.blueprints_updater.coordinator.async_get_translations"
    ) as mock_get:
        result = await coordinator.async_translate("test_key")
        assert result == "test_key"
        mock_get.assert_not_called()
