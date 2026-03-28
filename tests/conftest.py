from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def mock_asyncio_sleep():
    """Mock asyncio.sleep for all tests to run instantly."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


@pytest.fixture
def hass():
    """Mock HomeAssistant."""
    hass_mock = MagicMock(spec=HomeAssistant)
    hass_mock.config = MagicMock()
    hass_mock.config.path.return_value = "/config/blueprints"
    hass_mock.services = MagicMock()
    hass_mock.services.async_call = AsyncMock()

    async def async_add_executor_job(target, *args, **kwargs):
        return target(*args, **kwargs)

    hass_mock.async_add_executor_job = AsyncMock(side_effect=async_add_executor_job)
    hass_mock.data = {}
    return hass_mock
