"""Fixtures for coordinator tests."""

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)

from .utils import mock_bounded_response


@pytest.fixture
def hass(_mock_hass: HomeAssistant) -> HomeAssistant:
    """Aliasing _mock_hass to hass for unit tests."""
    return _mock_hass


@pytest.fixture
def coordinator(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> BlueprintUpdateCoordinator:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}
    coord = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=24),
    )

    def _mock_set_data(data: dict[str, dict[str, object]]) -> None:
        """Mock _mock_set_data."""
        coord.data = data

    monkeypatch.setattr(
        coord,
        "async_set_updated_data",
        MagicMock(side_effect=_mock_set_data),
    )
    monkeypatch.setattr(coord, "async_update_listeners", MagicMock())
    coord.setup_complete = True
    coord.last_update_success = True
    monkeypatch.setattr(coord, "_is_safe_path", MagicMock(return_value=True))
    monkeypatch.setattr(coord, "_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr(
        coord,
        "_async_get_bounded_response",
        AsyncMock(side_effect=mock_bounded_response),
    )
    return coord
