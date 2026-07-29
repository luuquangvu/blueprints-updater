"""Fixtures for integration tests."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


@pytest.fixture
def hass_fixture_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[bool]:
    """Prepare legacy HA test helpers with a per-test config directory."""

    def _get_test_config_dir(*parts: str) -> str:
        return str(tmp_path.joinpath(*parts))

    monkeypatch.setattr(
        "pytest_homeassistant_custom_component.common.get_test_config_dir",
        _get_test_config_dir,
    )
    return []


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Provide a per-test config directory for current HA test helpers."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all integration tests."""
    return


@pytest.fixture
def mock_storage() -> None:
    """Use Home Assistant's real Store implementation in integration tests."""
