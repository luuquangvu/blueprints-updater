"""Fixtures for UI tests."""

import pytest


@pytest.fixture
def hass(_mock_hass):
    """Aliasing _mock_hass to hass for unit tests."""
    return _mock_hass
