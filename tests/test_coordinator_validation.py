"""Tests for coordinator behavior during blueprint validation and hub interactions.

This module provides focused testing for the coordination logic between the
blueprints_updater integration and the Home Assistant blueprint hub, ensuring
robust fail-safe mechanisms are in place during compatibility checks.
"""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import BlueprintRiskType
from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator used in validation tests."""
    entry = MagicMock()
    entry.entry_id = "test_entry_validation"
    entry.options = {}
    entry.data = {}

    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__", return_value=None
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord.setup_complete = True
        coord.data = {}
        coord._translations = {}
        coord._blueprint_validate_lock = asyncio.Lock()
        return coord


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_hub_lifecycle(hass, coordinator):
    """Verify that blueprint consumer validation correctly manages the hub's temporary state.

    Ensures that the hub content is injected for validation and always restored to
    its original content (or removed if new) regardless of validation outcome.
    """
    rel_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"

    mock_hub = MagicMock()
    original_bp = MagicMock()
    mock_hub._blueprints = {"test.yaml": original_bp}

    hass.data["blueprint"] = {"automation": mock_hub}

    configs: dict[str, dict[str, Any]] = {
        "automation.test": {"alias": "Existing", "use_blueprint": {"path": rel_path, "input": {}}}
    }
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(),
    ) as mock_validate:

        async def check_during_validation(*args, **kwargs):
            assert mock_hub._blueprints["test.yaml"] != original_bp
            return None

        mock_validate.side_effect = check_during_validation

        risks = await coordinator._async_validate_blueprint_consumers(rel_path, content, configs)
        assert risks == []
        mock_validate.assert_awaited_once_with(
            hass,
            config_key="automation.test",
            config=configs["automation.test"],
        )

        assert mock_hub._blueprints["test.yaml"] == original_bp

    mock_hub._blueprints = {}
    with patch(
        "custom_components.blueprints_updater.coordinator.async_validate_automation_config",
        AsyncMock(side_effect=HomeAssistantError("Validation failed")),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(rel_path, content, configs)
        assert len(risks) == 1
        assert "Validation failed" in risks[0]["args"]["error"]
        assert "test.yaml" not in mock_hub._blueprints


@pytest.mark.asyncio
async def test_process_blueprint_content_error_branch_coverage(coordinator):
    """Verify that processing logic correctly categorizes different failure modes.

    Ensures that both structural dictionary checks and YAML syntax errors are
    accurately reflected in the blueprint's last_error state.
    """
    info: dict[str, Any] = {"rel_path": "test.yaml", "name": "Test BP", "local_hash": "old_hash"}

    path1 = "automation/invalid.yaml"
    coordinator.data[path1] = dict(info)
    await coordinator._process_blueprint_content(
        path1, info, "only_non_blueprint_data: True", "etag", "url", [], set()
    )
    assert coordinator.data[path1]["last_error"] == "invalid_blueprint"

    path2 = "automation/syntax.yaml"
    coordinator.data[path2] = dict(info)
    await coordinator._process_blueprint_content(
        path2, info, "invalid: yaml: [data", "etag", "url", [], set()
    )
    assert coordinator.data[path2]["last_error"].startswith("yaml_syntax_error|")

    path3 = "automation/schema.yaml"
    coordinator.data[path3] = dict(info)
    with patch(
        "custom_components.blueprints_updater.coordinator.Blueprint",
        side_effect=InvalidBlueprint("automation", "test", {}, "Mock Schema Failure"),
    ):
        await coordinator._process_blueprint_content(
            path3,
            info,
            "blueprint:\n  name: Test\n  domain: automation\n",
            "etag",
            "url",
            [],
            set(),
        )
        assert coordinator.data[path3]["last_error"].startswith("validation_error|")
        assert "Mock Schema Failure" in coordinator.data[path3]["last_error"]


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_unexpected_error(hass, coordinator):
    """Verify that unexpected errors during validation are caught and reported as SYSTEM_ERROR.

    Ensures that the catch-all Exception block handles internal logic failure gracefully.
    """
    rel_path = "automation/test.yaml"
    content = "blueprint:\n  name: test\n  domain: automation\n"
    configs: dict[str, dict[str, Any]] = {
        "automation.test": {
            "alias": "Existing",
            "use_blueprint": {"path": rel_path, "input": {}},
        }
    }

    with patch(
        "custom_components.blueprints_updater.coordinator.yaml_util.parse_yaml",
        side_effect=RuntimeError("Unexpected internal failure"),
    ):
        risks = await coordinator._async_validate_blueprint_consumers(rel_path, content, configs)
        assert len(risks) == 1
        assert risks[0]["type"] == BlueprintRiskType.SYSTEM_ERROR
        assert "Unexpected internal failure" in risks[0]["args"]["error"]


@pytest.mark.asyncio
async def test_async_validate_blueprint_consumers_malformed_path(coordinator):
    """Verify that a rel_path without a domain folder returns a SYSTEM_ERROR.

    Ensures that we don't silently skip validation or misparse filenames as domains.
    """
    rel_path = "invalid_path.yaml"  # Missing domain folder (e.g., automation/)
    content = "blueprint:\n  name: test\n  domain: automation\n"
    configs: dict[str, dict[str, Any]] = {}

    risks = await coordinator._async_validate_blueprint_consumers(rel_path, content, configs)

    assert len(risks) == 1
    assert risks[0]["type"] == BlueprintRiskType.SYSTEM_ERROR
    assert "Malformed blueprint path" in risks[0]["args"]["error"]
    assert risks[0]["args"]["path"] == rel_path
