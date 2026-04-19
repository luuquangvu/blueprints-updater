"""Tests for Blueprints Updater Advanced Compatibility Guard logic."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture(autouse=True)
def mock_frame_helper():
    """Mock HA frame helper to avoid setup errors."""
    with patch("homeassistant.helpers.frame.report_usage"):
        yield


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.options = {"auto_update": True}
    coord = BlueprintUpdateCoordinator(hass, config_entry, update_interval=timedelta(hours=1))
    coord.data = {}
    return coord


@pytest.mark.asyncio
async def test_detect_risks_for_update_compatibility_error(coordinator, hass):
    """Test detecting compatibility error during risk detection."""
    path = "automation/motion.yaml"
    info = {"rel_path": "automation/motion.yaml", "local_hash": "abc"}
    remote_content = (
        "blueprint:\n"
        "  name: New Motion\n"
        "  domain: automation\n"
        "  input:\n"
        "    sensor:\n"
        "      name: Sensor\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: binary_sensor"
    )
    last_error = None

    coordinator.data = {path: {}}

    blueprints_hub = MagicMock()
    blueprints_hub.async_get_blueprint = AsyncMock(return_value=None)
    blueprints_hub.async_add_blueprint = AsyncMock()
    blueprints_hub.async_remove_blueprint = AsyncMock()
    hass.data["blueprint"] = {"automation": blueprints_hub}

    entity_ids = ["automation.motion_light"]
    configs = {
        "automation.motion_light": {
            "use_blueprint": {
                "path": "automation/motion.yaml",
                "input": {"sensor": "binary_sensor.non_existent"},
            }
        }
    }

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.Blueprint", return_value=MagicMock()
        ),
        patch.object(coordinator, "_detect_breaking_changes", return_value=[]),
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=entity_ids),
        patch.object(coordinator, "_get_entities_configs", return_value=configs),
        patch(
            "homeassistant.components.automation.config.async_validate_config_item",
            new_callable=AsyncMock,
            side_effect=HomeAssistantError("Entity not found: binary_sensor.non_existent"),
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
            new_callable=AsyncMock,
            side_effect=HomeAssistantError("Entity not found: binary_sensor.non_existent"),
        ),
    ):
        risks = await coordinator._detect_risks_for_update(path, info, remote_content, last_error)

    matched = [r for r in risks if r["type"] == "compatibility_risk"]
    assert matched
    assert "Entity not found: binary_sensor.non_existent" in matched[0]["args"]["error"]


@pytest.mark.asyncio
async def test_detect_risks_for_update_invalid_blueprint(coordinator, hass):
    """Test detecting invalid blueprint content specifically."""
    path = "automation/motion.yaml"
    info = {"rel_path": "automation/motion.yaml", "local_hash": "abc"}
    remote_content = "invalid_yaml: ["
    last_error = None

    coordinator.data = {path: {}}

    with (
        patch("os.path.isfile", return_value=True),
        patch("builtins.open", create=True) as mock_open,
    ):
        mock_handle = mock_open.return_value.__enter__.return_value
        mock_handle.read.return_value = "blueprint:\n  name: Old"

        risks = await coordinator._detect_risks_for_update(path, info, remote_content, last_error)

    assert any(risk["type"] == "validation_failed_blueprint" for risk in risks)


@pytest.mark.asyncio
async def test_detect_risks_for_update_script_compatibility_error(coordinator, hass):
    """Test detecting compatibility error for scripts during risk detection."""
    path = "script/notification.yaml"
    info = {"rel_path": "script/notification.yaml", "local_hash": "abc"}
    remote_content = (
        "blueprint:\n"
        "  name: New Notify\n"
        "  domain: script\n"
        "  input:\n"
        "    target:\n"
        "      name: Target\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: mobile_app"
    )
    last_error = None

    coordinator.data = {path: {}}

    blueprints_hub = MagicMock()
    blueprints_hub.async_get_blueprint = AsyncMock(return_value=None)
    blueprints_hub.async_add_blueprint = AsyncMock()
    blueprints_hub.async_remove_blueprint = AsyncMock()
    hass.data["blueprint"] = {"script": blueprints_hub}

    entity_ids = ["script.notify_me"]
    configs = {
        "script.notify_me": {
            "use_blueprint": {
                "path": "script/notification.yaml",
                "input": {"target": "mobile_app.non_existent"},
            }
        }
    }

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.Blueprint", return_value=MagicMock()
        ),
        patch.object(coordinator, "_detect_breaking_changes", return_value=[]),
        patch.object(coordinator, "_get_entities_using_blueprint_list", return_value=entity_ids),
        patch.object(coordinator, "_get_entities_configs", return_value=configs),
        patch(
            "homeassistant.components.script.config.async_validate_config_item",
            new_callable=AsyncMock,
            side_effect=HomeAssistantError("Entity not found: mobile_app.non_existent"),
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.async_validate_script_config",
            new_callable=AsyncMock,
            side_effect=HomeAssistantError("Entity not found: mobile_app.non_existent"),
        ),
    ):
        risks = await coordinator._detect_risks_for_update(path, info, remote_content, last_error)

    matched = [r for r in risks if r["type"] == "compatibility_risk"]
    assert matched
    assert "script.notify_me" in matched[0]["args"]["entity"]
    assert "Entity not found: mobile_app.non_existent" in matched[0]["args"]["error"]


def test_dedupe_risks(coordinator):
    """Test the _dedupe_risks helper logic."""
    risks = [
        "Legacy risk string",
        "Legacy risk string",
        {"type": "new_mandatory", "args": {"input": "input1"}},
        {"type": "new_mandatory", "args": {"input": "input1"}},
        {"type": "new_mandatory", "args": {"input": "input2"}},
        {"type": "missing_input", "args": {"input": "input1", "entity": "e1"}},
        {"malformed": "risk"},
    ]

    deduped = coordinator._dedupe_risks(risks)

    assert len(deduped) == 4
    assert any(
        r["type"] == "legacy_risk" and r["args"]["message"] == "Legacy risk string" for r in deduped
    )
    assert any(r["type"] == "new_mandatory" and r["args"]["input"] == "input1" for r in deduped)
    assert any(r["type"] == "new_mandatory" and r["args"]["input"] == "input2" for r in deduped)
    assert any(
        r["type"] == "missing_input"
        and r["args"]["entity"] == "e1"
        and r["args"]["input"] == "input1"
        for r in deduped
    )

    def count_matches(rtype, rargs):
        return sum(r["type"] == rtype and r["args"] == rargs for r in deduped)

    assert count_matches("new_mandatory", {"input": "input1"}) == 1
    assert count_matches("legacy_risk", {"message": "Legacy risk string"}) == 1
