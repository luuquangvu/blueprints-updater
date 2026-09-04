"""Integration tests for supported Home Assistant test environments."""

from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.components.automation import automations_with_blueprint
from homeassistant.components.script import scripts_with_blueprint
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
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
async def test_live_automation_blueprint_consumer_discovery(
    hass: HomeAssistant,
) -> None:
    """Recover original inputs from a live HA automation blueprint consumer."""
    blueprint_path = Path(hass.config.path("blueprints", "automation", "compat.yaml"))
    blueprint_path.parent.mkdir(parents=True, exist_ok=True)
    blueprint_path.write_text(
        "blueprint:\n"
        "  name: Compatibility Consumer\n"
        "  domain: automation\n"
        "  input:\n"
        "    target_boolean:\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: input_boolean\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: input_boolean.trigger_test\n"
        "action:\n"
        "  - service: input_boolean.turn_on\n"
        "    target:\n"
        "      entity_id: !input target_boolean\n",
        encoding="utf-8",
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "compatibility_consumer",
                    "alias": "Compatibility Consumer",
                    "use_blueprint": {
                        "path": "compat.yaml",
                        "input": {"target_boolean": "input_boolean.target_test"},
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()

    entity_ids = automations_with_blueprint(hass, "compat.yaml")
    assert len(entity_ids) == 1

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="live_consumer_entry",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    configs = coordinator._get_entities_configs(entity_ids)

    assert configs[entity_ids[0]]["use_blueprint"] == {
        "path": "compat.yaml",
        "input": {"target_boolean": "input_boolean.target_test"},
    }


@pytest.mark.asyncio
async def test_live_script_blueprint_consumer_discovery(hass: HomeAssistant) -> None:
    """Recover original inputs from a live HA script blueprint consumer."""
    blueprint_path = Path(hass.config.path("blueprints", "script", "compat.yaml"))
    blueprint_path.parent.mkdir(parents=True, exist_ok=True)
    blueprint_path.write_text(
        "blueprint:\n"
        "  name: Compatibility Script Consumer\n"
        "  domain: script\n"
        "  input:\n"
        "    delay_seconds:\n"
        "      default: 1\n"
        "      selector:\n"
        "        number:\n"
        "          min: 1\n"
        "          max: 60\n"
        "sequence:\n"
        "  - delay:\n"
        "      seconds: !input delay_seconds\n",
        encoding="utf-8",
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "compatibility_consumer": {
                    "alias": "Compatibility Script Consumer",
                    "use_blueprint": {
                        "path": "compat.yaml",
                        "input": {"delay_seconds": 5},
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()

    entity_ids = scripts_with_blueprint(hass, "compat.yaml")
    assert len(entity_ids) == 1

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="live_script_consumer_entry",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    configs = coordinator._get_entities_configs(entity_ids)

    assert configs[entity_ids[0]]["use_blueprint"] == {
        "path": "compat.yaml",
        "input": {"delay_seconds": 5},
    }


@pytest.mark.asyncio
async def test_unmocked_translation_helper_contract(hass: HomeAssistant) -> None:
    """Load an integration translation through HA's real translation helper."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="translation_contract_entry",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
    coordinator.setup_complete = True

    assert await coordinator.async_translate("up_to_date") == "Up to date"


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


@pytest.mark.asyncio
async def test_proactive_default_fallback_simulation_catches_unsafe_empty_default(
    hass: HomeAssistant,
) -> None:
    """Active consumer with valid input fails when default fallback violates HA schema."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_fallback_val",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    unsafe_blueprint = (
        "blueprint:\n"
        "  name: Unsafe Action Target Blueprint\n"
        "  domain: automation\n"
        "  input:\n"
        "    trigger_bool:\n"
        "      name: Trigger Boolean\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: input_boolean\n"
        "    optional_helper:\n"
        "      name: Optional Helper\n"
        "      default: ''\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: input_boolean\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: !input trigger_bool\n"
        "action:\n"
        "  - service: input_boolean.turn_on\n"
        "    target:\n"
        "      entity_id: !input optional_helper\n"
    )

    # Consumer explicitly supplied optional_helper, so direct evaluation succeeds
    consumer_config: dict[str, object] = {
        "use_blueprint": {
            "path": "automation/unsafe_bp.yaml",
            "input": {
                "trigger_bool": "input_boolean.trigger",
                "optional_helper": "input_boolean.target",
            },
        }
    }

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="automation/unsafe_bp.yaml",
        blueprint_content=unsafe_blueprint,
        configs={"automation.test_instance": consumer_config},
    )

    # Default-fallback simulation catches that omitting optional_helper fails schema
    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
    assert risks[0]["args"]["entity"] == "automation.test_instance"


@pytest.mark.asyncio
async def test_proactive_baseline_simulation_zero_consumers_catches_unsafe_empty_default(
    hass: HomeAssistant,
) -> None:
    """Zero-consumer blueprint with unsafe default input fails proactive baseline simulation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_zero_consumers",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    unsafe_blueprint = (
        "blueprint:\n"
        "  name: Unsafe Zero Consumer Blueprint\n"
        "  domain: automation\n"
        "  input:\n"
        "    optional_helper:\n"
        "      name: Optional Helper\n"
        "      default: ''\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: input_boolean\n"
        "trigger:\n"
        "  - platform: homeassistant\n"
        "    event: start\n"
        "action:\n"
        "  - service: input_boolean.turn_on\n"
        "    target:\n"
        "      entity_id: !input optional_helper\n"
    )

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="automation/unsafe_zero.yaml",
        blueprint_content=unsafe_blueprint,
        configs={},
    )

    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
    assert risks[0]["args"]["entity"] == "automation/unsafe_zero.yaml"


@pytest.mark.asyncio
async def test_proactive_simulation_allows_standard_target_selector_default_dict(
    hass: HomeAssistant,
) -> None:
    """Standard target selector with default: {} is valid in Home Assistant Core."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_target_dict",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_blueprint = (
        "blueprint:\n"
        "  name: Valid Target Dict Blueprint\n"
        "  domain: automation\n"
        "  input:\n"
        "    target_dev:\n"
        "      name: Target Device\n"
        "      default: {}\n"
        "      selector:\n"
        "        target:\n"
        "trigger:\n"
        "  - platform: homeassistant\n"
        "    event: start\n"
        "action:\n"
        "  - service: homeassistant.turn_on\n"
        "    target: !input target_dev\n"
    )

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="automation/valid_target.yaml",
        blueprint_content=valid_blueprint,
        configs={},
    )

    assert risks == []


@pytest.mark.asyncio
async def test_proactive_baseline_simulation_zero_consumers_valid_script(
    hass: HomeAssistant,
) -> None:
    """Zero-consumer script blueprint validates successfully in proactive baseline simulation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_zero_script",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_script_bp = (
        "blueprint:\n"
        "  name: Valid Zero Consumer Script Blueprint\n"
        "  domain: script\n"
        "  input:\n"
        "    delay_seconds:\n"
        "      name: Delay\n"
        "      default: 5\n"
        "      selector:\n"
        "        number:\n"
        "          min: 1\n"
        "          max: 60\n"
        "sequence:\n"
        "  - delay:\n"
        "      seconds: !input delay_seconds\n"
    )

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="script/valid_zero_script.yaml",
        blueprint_content=valid_script_bp,
        configs={},
    )

    assert risks == []


@pytest.mark.asyncio
async def test_proactive_baseline_simulation_zero_consumers_catches_invalid_script(
    hass: HomeAssistant,
) -> None:
    """Zero-consumer script blueprint with invalid action fails baseline simulation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_zero_script_invalid",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    invalid_script_bp = (
        "blueprint:\n"
        "  name: Invalid Zero Consumer Script\n"
        "  domain: script\n"
        "sequence:\n"
        "  - service: ''\n"
    )

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="script/invalid_zero.yaml",
        blueprint_content=invalid_script_bp,
        configs={},
    )

    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.COMPATIBILITY
    assert risks[0]["args"]["entity"] == "script/invalid_zero.yaml"


@pytest.mark.asyncio
async def test_proactive_baseline_simulation_zero_consumers_valid_template(
    hass: HomeAssistant,
) -> None:
    """Zero-consumer template blueprint validates successfully in proactive baseline simulation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"update_interval": 24},
        entry_id="test_compat_zero_template",
    )
    entry.add_to_hass(hass)
    coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    valid_template_bp = (
        "blueprint:\n"
        "  name: Valid Zero Consumer Template\n"
        "  domain: template\n"
        "  input:\n"
        "    sensor_name:\n"
        "      name: Sensor Name\n"
        "      default: My Template Sensor\n"
        "      selector:\n"
        "        text:\n"
        "sensor:\n"
        "  - name: !input sensor_name\n"
        '    state: "OK"\n'
    )

    risks = await coordinator._async_validate_blueprint_consumers(
        relative_path="template/valid_zero.yaml",
        blueprint_content=valid_template_bp,
        configs={},
    )

    assert risks == []
