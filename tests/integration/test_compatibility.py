"""Integration tests for supported Home Assistant test environments."""

from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater.const import DOMAIN, BlueprintRiskType
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


async def test_hass_uses_isolated_config_directory(
    hass: HomeAssistant,
    tmp_path: Path,
) -> None:
    """Keep legacy and current HA test runs isolated from shared package data."""
    assert Path(hass.config.config_dir) == tmp_path


@pytest.mark.asyncio
async def test_unmocked_automation_blueprint_consumer_validation(
    hass: HomeAssistant,
) -> None:
    """Validate automation blueprint input substitution against real HA Core validators."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_auto_val",
    )
    entry.add_to_hass(hass)

    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_automation_bp = (
        "blueprint:\n"
        "  name: Test Automation Blueprint\n"
        "  domain: automation\n"
        "  input:\n"
        "    target_boolean:\n"
        "      name: Target Boolean\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: input_boolean\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: input_boolean.trigger_test\n"
        "action:\n"
        "  - service: input_boolean.turn_on\n"
        "    target:\n"
        "      entity_id: !input target_boolean\n"
    )

    valid_consumer_config: dict[str, object] = {
        "use_blueprint": {
            "path": "automation/test_auto.yaml",
            "input": {
                "target_boolean": "input_boolean.target_test",
            },
        }
    }

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="automation/test_auto.yaml",
        blueprint_content=valid_automation_bp,
        configs={"automation.test_instance": valid_consumer_config},
    )

    assert risks == []

    invalid_consumer_config: dict[str, object] = {
        "use_blueprint": {
            "path": "automation/test_auto.yaml",
            "input": {},
        }
    }

    invalid_risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="automation/test_auto.yaml",
        blueprint_content=valid_automation_bp,
        configs={"automation.test_instance": invalid_consumer_config},
    )

    assert len(invalid_risks) == 1
    assert invalid_risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
    assert invalid_risks[0]["args"]["entity"] == "automation.test_instance"


@pytest.mark.asyncio
async def test_unmocked_script_blueprint_consumer_validation(
    hass: HomeAssistant,
) -> None:
    """Validate script blueprint input substitution against real HA Core validators."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_script_val",
    )
    entry.add_to_hass(hass)

    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_script_bp = (
        "blueprint:\n"
        "  name: Test Script Blueprint\n"
        "  domain: script\n"
        "  input:\n"
        "    delay_seconds:\n"
        "      name: Delay\n"
        "      default: 1\n"
        "      selector:\n"
        "        number:\n"
        "          min: 1\n"
        "          max: 60\n"
        "sequence:\n"
        "  - delay:\n"
        "      seconds: !input delay_seconds\n"
    )

    valid_script_config: dict[str, object] = {
        "use_blueprint": {
            "path": "script/test_script.yaml",
            "input": {
                "delay_seconds": 5,
            },
        }
    }

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="script/test_script.yaml",
        blueprint_content=valid_script_bp,
        configs={"script.test_script_instance": valid_script_config},
    )

    assert risks == []


@pytest.mark.asyncio
async def test_unmocked_template_blueprint_consumer_validation(
    hass: HomeAssistant,
) -> None:
    """Validate template blueprint input substitution against real HA Core validators."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_template_val",
    )
    entry.add_to_hass(hass)

    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_template_bp = (
        "blueprint:\n"
        "  name: Test Template Blueprint\n"
        "  domain: template\n"
        "  input:\n"
        "    sensor_name:\n"
        "      name: Sensor Name\n"
        "      selector:\n"
        "        text:\n"
        "sensor:\n"
        "  - name: !input sensor_name\n"
        '    state: "OK"\n'
    )

    valid_template_config: dict[str, object] = {
        "use_blueprint": {
            "path": "template/test_template.yaml",
            "input": {
                "sensor_name": "My Template Sensor",
            },
        }
    }

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="template/test_template.yaml",
        blueprint_content=valid_template_bp,
        configs={"template.my_sensor": valid_template_config},
    )

    assert risks == []
