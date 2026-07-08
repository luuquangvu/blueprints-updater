"""Fixtures for coordinator tests."""

from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)

from .protocols import BlueprintCoordinatorProtocol


@pytest.fixture
def hass(_mock_hass):
    """Aliasing _mock_hass to hass for unit tests."""
    return _mock_hass


@pytest.fixture
def coordinator(hass) -> BlueprintCoordinatorProtocol:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}
    coord = cast(
        BlueprintCoordinatorProtocol,
        BlueprintUpdateCoordinator(
            hass,
            entry,
            timedelta(hours=24),
        ),
    )

    coord.hass = hass
    coord.data = {}

    def _mock_set_data(data):
        """Mock _mock_set_data."""
        coord.data = data

    coord_any = cast(Any, coord)
    coord_any.async_set_updated_data = MagicMock(side_effect=_mock_set_data)
    coord_any.async_update_listeners = MagicMock()
    coord_any.setup_complete = True
    coord_any.last_update_success = True
    coord_any._is_safe_path = MagicMock(return_value=True)
    coord_any._is_safe_url = AsyncMock(return_value=True)
    return coord
