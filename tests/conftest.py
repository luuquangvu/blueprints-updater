"""Fixtures for Blueprints Updater tests."""

from typing import Any, Protocol, runtime_checkable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant


@runtime_checkable
class BlueprintCoordinatorProtocol(Protocol):
    """Protocol for BlueprintUpdateCoordinator testing.

    This protocol exposes private methods and attributes to the test suite
    in a type-safe manner, avoiding the need for # type: ignore or cast(Any, ...).
    """

    hass: Any
    config_entry: Any
    data: dict[str, Any]
    setup_complete: bool

    _listeners: dict[Any, Any]
    _persisted_etags: dict[str, str]
    _persisted_hashes: dict[str, str]

    async_set_updated_data: MagicMock
    async_update_listeners: MagicMock
    _is_safe_path: MagicMock
    _is_safe_url: AsyncMock
    _store: MagicMock
    _async_update_blueprint_in_place: AsyncMock
    _async_save_metadata: AsyncMock
    _async_background_refresh: AsyncMock
    _validate_blueprint: MagicMock
    async_install_blueprint: AsyncMock
    async_reload_services: AsyncMock
    async_translate: AsyncMock
    _process_blueprint_content: AsyncMock
    _start_background_refresh: AsyncMock
    async_setup: AsyncMock
    async_refresh: AsyncMock
    scan_blueprints: MagicMock


@pytest.fixture(autouse=True)
def mock_asyncio_sleep():
    """Mock asyncio.sleep for all tests to run instantly."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


@pytest.fixture(autouse=True)
def mock_storage():
    """Mock Home Assistant storage."""
    with patch("custom_components.blueprints_updater.coordinator.Store") as mock_store:
        mock_store.return_value.async_load = AsyncMock(return_value={})
        mock_store.return_value.async_save = AsyncMock()
        yield mock_store


@pytest.fixture
def hass():
    """Mock HomeAssistant."""
    hass_mock = MagicMock(spec=HomeAssistant)
    hass_mock.config = MagicMock()
    hass_mock.config.path.return_value = "/config/blueprints"
    hass_mock.services = MagicMock()
    hass_mock.services.async_call = AsyncMock()

    hass_mock.bus = MagicMock()
    hass_mock.bus.async_listen = MagicMock()

    async def async_add_executor_job(target, *args, **kwargs):
        return target(*args, **kwargs)

    hass_mock.async_add_executor_job = AsyncMock(side_effect=async_add_executor_job)

    def async_create_background_task(coro, name=None):
        """Mock creating a background task."""
        import asyncio

        return asyncio.create_task(coro, name=name)

    hass_mock.async_create_background_task = MagicMock(side_effect=async_create_background_task)

    hass_mock.data = {}
    return hass_mock
