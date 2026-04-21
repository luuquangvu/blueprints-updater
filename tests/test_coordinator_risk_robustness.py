"""Tests for coordinator risk detection robustness."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.blueprints_updater.const import DOMAIN
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.mark.asyncio
async def test_detect_risks_system_error_on_exception(hass):
    """Test that exceptions during risk detection result in a system_error risk."""
    entry = MagicMock()
    entry.domain = DOMAIN
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    path = "/config/blueprints/automation/test.yaml"
    rel_path = "automation/test.yaml"
    info = {"rel_path": rel_path, "name": "Test Blueprint"}
    coordinator.data = {path: info}

    remote_content = "blueprint:\n  name: New"

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("builtins.open", side_effect=Exception("Test Exception")),
    ):
        risks = await coordinator._detect_risks_for_update(path, info, remote_content, None)

    assert len(risks) == 1
    assert risks[0]["type"] == "system_error"
    assert risks[0]["args"]["error"] == "Test Exception"
    assert risks[0]["args"]["rel_path"] == rel_path


@pytest.mark.asyncio
async def test_detect_risks_missing_rel_path(hass):
    """Test that missing rel_path results in a system_error risk."""
    entry = MagicMock()
    entry.domain = DOMAIN
    with patch.object(DataUpdateCoordinator, "__init__", return_value=None):
        coordinator = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))

    path = "/config/blueprints/automation/test.yaml"
    info = {"name": "Test Blueprint"}
    coordinator.data = {path: info}

    risks = await coordinator._detect_risks_for_update(path, info, "content", None)

    assert len(risks) == 1
    assert risks[0]["type"] == "system_error"
    assert risks[0]["args"]["error"] == "missing_path"
    assert risks[0]["args"]["path"] == "test.yaml"
