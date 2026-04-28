"""Tests for Blueprints Updater ETag logic."""

from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.core import HomeAssistant

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator

from .protocols import BlueprintCoordinatorProtocol


@pytest.fixture
def coordinator(hass) -> BlueprintCoordinatorProtocol:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
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
        coord.async_set_updated_data = cast(Any, MagicMock(return_value=None))
        coord.setup_complete = True
        coord._is_safe_path = cast(Any, MagicMock(return_value=True))
        coord._is_safe_url = cast(Any, AsyncMock(return_value=True))
        return coord


@pytest.mark.asyncio
async def test_304_response_preserves_updatable_status(
    hass: HomeAssistant, coordinator: BlueprintCoordinatorProtocol
):
    """Test that a 304 response doesn't flip 'Update available' back to 'Up to date'."""
    path = "/config/blueprints/test.yaml"
    local_content = "blueprint:\n  name: Old"
    remote_content = "blueprint:\n  name: New"

    url = "https://github.com/user/repo/test.yaml"
    local_hash = coordinator._hash_content(local_content, url)
    remote_hash = coordinator._hash_content(remote_content, url)

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
    mock_response.status_code = HTTPStatus.NOT_MODIFIED
    mock_response.url = httpx.URL("https://github.com/user/repo/test.yaml")
    mock_response.headers = httpx.Headers({"ETag": "etag_v2", "Content-Type": "text/yaml"})
    mock_response.raise_for_status = MagicMock(return_value=None)

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)
    mock_session.send = AsyncMock(return_value=mock_response)

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
    hass: HomeAssistant, coordinator: BlueprintCoordinatorProtocol
):
    """Test that remote hashes are correctly saved and restored."""
    path = "/config/blueprints/test.yaml"
    remote_hash = "some_remote_hash"
    etag = "some_etag"

    coordinator.data = {
        path: {
            "rel_path": "test.yaml",
            "remote_hash": remote_hash,
            "etag": etag,
            "source_url": "https://url",
        }
    }

    mock_store = MagicMock()
    mock_store.async_save = AsyncMock(return_value=None)
    coordinator._store = mock_store
    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True
    ):
        await coordinator._async_save_metadata()

    save_args = mock_store.async_save.call_args[0][0]
    assert save_args["metadata"]["test.yaml"]["etag"] == etag
    assert save_args["metadata"]["test.yaml"]["remote_hash"] == remote_hash

    coordinator._persisted_metadata = {}
    mock_store.async_load = AsyncMock(return_value=save_args)

    await coordinator.async_setup()

    mock_store.async_load.assert_awaited_once()
    assert coordinator._persisted_metadata["test.yaml"]["etag"] == etag
    assert coordinator._persisted_metadata["test.yaml"]["remote_hash"] == remote_hash


@pytest.mark.asyncio
async def test_etag_migration_forces_download(
    hass: HomeAssistant, coordinator: BlueprintCoordinatorProtocol
):
    """Test that if remote_hash is missing from persisted data, the ETag is ignored."""
    path = "/config/blueprints/test.yaml"
    remote_content = "blueprint:\n  name: fresh\n  domain: automation"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "domain": "automation",
        "source_url": "https://github.com/user/repo/bp.yaml",
        "local_hash": "stale_hash",
    }
    remote_hash = coordinator._hash_content(remote_content, info["source_url"])

    coordinator.data = {
        path: {
            "name": "Test",
            "domain": "automation",
            "etag": "old_etag",
            "remote_hash": None,
        }
    }

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = HTTPStatus.OK
    mock_response.url = httpx.URL("https://github.com/user/repo/bp.yaml")
    mock_response.headers = httpx.Headers({"ETag": "new_etag", "Content-Type": "text/yaml"})
    mock_response.text = remote_content
    mock_response.raise_for_status = MagicMock(return_value=None)

    mock_session = MagicMock(spec=httpx.AsyncClient)
    mock_session.get = AsyncMock(return_value=mock_response)
    mock_session.send = AsyncMock(return_value=mock_response)

    results_to_notify: list[str] = []
    updated_domains: set[str] = set()
    await coordinator._async_update_blueprint_in_place(
        mock_session, path, info, results_to_notify, updated_domains
    )

    _args, kwargs = mock_session.get.call_args
    assert "If-None-Match" not in kwargs.get("headers", {})

    assert coordinator.data[path]["remote_hash"] == remote_hash
    assert coordinator.data[path]["updatable"] is True


@pytest.mark.asyncio
async def test_async_save_metadata_preserves_persisted_entries_for_existing_files(coordinator):
    """Test that persisted entries for existing files are preserved even if not in current scan."""
    existing_path = "/config/blueprints/existing.yaml"

    coordinator._persisted_metadata = {
        "existing.yaml": {
            "etag": "etag-existing",
            "remote_hash": "hash-existing",
            "source_url": "https://url",
        },
        "orphaned.yaml": {
            "etag": "etag-orphaned",
            "remote_hash": "hash-orphaned",
            "source_url": "https://url",
        },
    }

    coordinator.data = {
        existing_path: {
            "rel_path": "existing.yaml",
            "etag": "etag-new",
            "remote_hash": "hash-new",
            "source_url": "https://url",
        }
    }

    mock_store = MagicMock()
    mock_store.async_save = AsyncMock(return_value=None)
    coordinator._store = mock_store

    with patch.object(
        coordinator,
        "_filter_existing_metadata",
        side_effect=lambda root, meta: {
            k: v for k, v in meta.items() if k in ("existing.yaml", "orphaned.yaml")
        },
    ):
        await coordinator._async_save_metadata()

    mock_store.async_save.assert_awaited_once()
    save_args = mock_store.async_save.call_args[0][0]
    assert save_args["metadata"]["existing.yaml"]["etag"] == "etag-new"
    assert save_args["metadata"]["existing.yaml"]["remote_hash"] == "hash-new"
    assert save_args["metadata"]["orphaned.yaml"]["etag"] == "etag-orphaned"
    assert save_args["metadata"]["orphaned.yaml"]["remote_hash"] == "hash-orphaned"


@pytest.mark.asyncio
async def test_async_save_metadata_force_true_persists_even_when_unchanged(coordinator):
    """Test that force=True bypasses equality checks."""
    path = "/config/blueprints/test.yaml"
    etag = "etag-123"
    file_hash = "hash-123"

    coordinator._persisted_metadata = {
        "test.yaml": {"etag": etag, "remote_hash": file_hash, "source_url": "https://url"}
    }
    coordinator.data = {
        path: {
            "rel_path": "test.yaml",
            "etag": etag,
            "remote_hash": file_hash,
            "source_url": "https://url",
        }
    }

    mock_store = MagicMock()
    mock_store.async_save = AsyncMock(return_value=None)
    coordinator._store = mock_store

    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.isfile",
        return_value=True,
    ):
        await coordinator._async_save_metadata(force=True)

    mock_store.async_save.assert_awaited_once()
