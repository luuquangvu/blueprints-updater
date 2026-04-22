"""Fixtures for coordinator tests."""

from datetime import timedelta
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.blueprints_updater.coordinator import (
    BlueprintUpdateCoordinator,
)

from .protocols import (
    BlueprintCoordinatorProtocol,
)


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

    coord.async_set_updated_data = cast(Any, MagicMock(side_effect=_mock_set_data))
    coord.async_update_listeners = cast(Any, MagicMock())
    coord.setup_complete = True
    coord.last_update_success = True
    coord._is_safe_path = cast(Any, MagicMock(return_value=True))
    coord._is_safe_url = cast(Any, AsyncMock(return_value=True))
    return coord
