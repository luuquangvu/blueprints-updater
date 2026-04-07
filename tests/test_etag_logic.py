"""Tests for Blueprints Updater ETag logic."""

import hashlib
from datetime import timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from conftest import BlueprintCoordinatorProtocol
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass) -> BlueprintCoordinatorProtocol:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {"auto_update": False}
    entry.data = {}

    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
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
        coord.async_set_updated_data = MagicMock()
        coord.setup_complete = True
        coord._is_safe_path = MagicMock(return_value=True)
        coord._is_safe_url = AsyncMock(return_value=True)
        return coord


@pytest.mark.asyncio
async def test_304_response_preserves_updatable_status(
    hass: HomeAssistant, coordinator: BlueprintUpdateCoordinator
):
    """Test that a 304 response doesn't flip 'Update available' back to 'Up to date'.

    This occurs if the local file hasn't been updated.
    """
    path = "/config/blueprints/test.yaml"
    local_content = "blueprint:\n  name: Old"
    remote_content = "blueprint:\n  name: New"

    local_hash = hashlib.sha256(local_content.encode()).hexdigest()
    remote_hash = hashlib.sha256(remote_content.encode()).hexdigest()

    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "domain": "automation",
        "source_url": "https://github.com/user/repo/test.yaml",
        "local_hash": local_hash,
    }

    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
            "domain": "automation",
            "source_url": "https://github.com/user/repo/test.yaml",
            "local_hash": local_hash,
            "updatable": True,
            "remote_hash": remote_hash,
            "etag": "etag_v2",
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 304
    mock_response.headers = {"ETag": "etag_v2"}
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    results_to_notify: list[str] = []
    updated_domains: set[str] = set()
    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )

    assert coordinator.data[path]["updatable"] is True
    assert coordinator.data[path]["remote_hash"] == remote_hash
    assert coordinator.data[path]["etag"] == "etag_v2"


@pytest.mark.asyncio
async def test_persistence_of_remote_hashes(
    hass: HomeAssistant, coordinator: BlueprintUpdateCoordinator
):
    """Test that remote hashes are correctly saved and restored."""
    path = "/config/blueprints/test.yaml"
    remote_hash = "some_remote_hash"
    etag = "some_etag"

    coordinator.data = {
        path: {
            "remote_hash": remote_hash,
            "etag": etag,
        }
    }

    mock_store = MagicMock()
    mock_store.async_save = AsyncMock()
    coordinator._store = mock_store
    await coordinator._async_save_metadata()

    save_args = mock_store.async_save.call_args[0][0]
    assert save_args["etags"][path] == etag
    assert save_args["remote_hashes"][path] == remote_hash

    coordinator._persisted_etags = {}
    coordinator._persisted_hashes = {}
    mock_store.async_load = AsyncMock(return_value=save_args)

    await coordinator.async_setup()

    mock_store.async_load.assert_awaited_once()
    assert coordinator._persisted_etags[path] == etag
    assert coordinator._persisted_hashes[path] == remote_hash


@pytest.mark.asyncio
async def test_etag_migration_forces_download(
    hass: HomeAssistant, coordinator: BlueprintUpdateCoordinator
):
    """Test that if remote_hash is missing from persisted data.

    The ETag is ignored to force a full download and populate the hash.
    """
    path = "/config/blueprints/test.yaml"
    remote_content = "blueprint:\n  name: fresh\n  domain: automation"
    remote_hash = hashlib.sha256(remote_content.encode()).hexdigest()

    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "domain": "automation",
        "source_url": "https://github.com/user/repo/bp.yaml",
        "local_hash": "stale_hash",
    }

    coordinator.data = {
        path: {
            "name": "Test",
            "domain": "automation",
            "etag": "old_etag",
            "remote_hash": None,
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"ETag": "new_etag"}
    mock_response.text = remote_content
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)

    results_to_notify: list[str] = []
    updated_domains: set[str] = set()
    with patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_sha:
        mock_sha.return_value.hexdigest.return_value = remote_hash
        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify, updated_domains
        )

    _args, kwargs = mock_session.get.call_args
    assert "If-None-Match" not in kwargs.get("headers", {})

    assert coordinator.data[path]["remote_hash"] == remote_hash
    assert coordinator.data[path]["updatable"] is True
