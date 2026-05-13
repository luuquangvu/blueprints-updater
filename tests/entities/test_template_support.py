"""Tests for template blueprint support."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.update import BlueprintUpdateEntity


@pytest.mark.asyncio
async def test_template_usage_warning(hass: HomeAssistant):
    """Test that template usage is correctly identified and warning is generated."""
    coordinator = MagicMock(spec=BlueprintUpdateCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.data = {
        "/config/blueprints/template/test.yaml": {
            "name": "Test Template",
            "relative_path": "template/test.yaml",
            "updatable": True,
            "source_url": "https://github.com/user/repo/template.yaml",
        }
    }
    coordinator.async_translate = AsyncMock(side_effect=lambda key, **kwargs: f"[{key}]")
    coordinator._normalize_domain = MagicMock(side_effect=lambda x: x)

    entity = BlueprintUpdateEntity(
        coordinator,
        "/config/blueprints/template/test.yaml",
        coordinator.data["/config/blueprints/template/test.yaml"],
    )

    with patch(
        "custom_components.blueprints_updater.update.templates_with_blueprint",
        return_value=["template.test_entity"],
    ) as mock_templates_with_blueprint:
        notes = await entity.async_generate_release_notes()
        assert notes is not None
        assert "[usage_warning]" in notes
        mock_templates_with_blueprint.assert_called_with(hass, "test.yaml")


@pytest.mark.asyncio
async def test_template_config_extraction(hass: HomeAssistant):
    """Test that configuration is correctly extracted from template entities."""
    coordinator = BlueprintUpdateCoordinator(hass, MagicMock(), timedelta(hours=24))

    mock_template_entity = MagicMock()
    mock_template_entity.entity_id = "template.my_entity"
    mock_template_entity._blueprint_inputs = {
        "use_blueprint": {"path": "test.yaml", "input": {"my_input": "my_value"}}
    }

    configs: dict = {}
    coordinator._populate_config_from_entity(mock_template_entity, "template.my_entity", configs)

    assert "template.my_entity" in configs
    assert configs["template.my_entity"]["use_blueprint"]["input"]["my_input"] == "my_value"
