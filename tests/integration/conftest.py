"""Fixtures for Home Assistant integration tests."""

from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blueprints_updater import coordinator as coordinator_module
from custom_components.blueprints_updater.const import DOMAIN


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
    """Provide a per-test Home Assistant config directory."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for every integration test."""


@pytest.fixture(autouse=True)
def real_coordinator_store(mock_coordinator_store: MagicMock) -> Generator[None]:
    """Ensure integration tests use real Home Assistant storage without mock interference."""
    del mock_coordinator_store
    with patch.object(coordinator_module, "Store", Store):
        assert coordinator_module.Store is Store, (
            "Expected real Home Assistant Store in integration tests, but Store is mocked"
        )
        yield


@pytest.fixture
def create_blueprint(
    hass: HomeAssistant,
) -> Callable[[str, str], str]:
    """Helper fixture to create a blueprint file in the HA config directory."""

    def _create(relative_path: str, content: str) -> str:
        blueprints_dir = Path(hass.config.path("blueprints"))
        full_path = blueprints_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return str(full_path)

    return _create


@asynccontextmanager
async def async_setup_integration_entry(
    hass: HomeAssistant,
    *,
    entry_id: str = "test_entry",
    options: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up a MockConfigEntry with background refresh patched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data or {},
        options=options if options is not None else {"update_interval": 24},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._async_background_refresh"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            yield entry
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


@pytest.fixture
def setup_integration_entry(
    hass: HomeAssistant,
) -> Callable[..., AbstractAsyncContextManager[MockConfigEntry]]:
    """Fixture providing an async context manager factory for setting up an integration entry."""

    def _factory(
        *,
        entry_id: str = "test_entry",
        options: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> AbstractAsyncContextManager[MockConfigEntry]:
        return async_setup_integration_entry(
            hass,
            entry_id=entry_id,
            options=options,
            data=data,
        )

    return _factory


@pytest.fixture
async def setup_integration(hass: HomeAssistant) -> AsyncGenerator[MockConfigEntry]:
    """Set up the default integration entry for tests."""
    async with async_setup_integration_entry(hass) as entry:
        yield entry
