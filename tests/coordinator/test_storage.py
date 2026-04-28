"""Tests for coordinator storage, persistence, and metadata pruning."""

import asyncio
import os
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.mark.asyncio
async def test_prune_preserves_hashes_only_metadata(coordinator):
    """Test that blueprints with only a hash (and no ETag) are not pruned if they exist."""
    coordinator._persisted_metadata = {"automation/hash_only.yaml": {"remote_hash": "some_hash"}}

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch.object(coordinator, "_async_save_metadata") as mock_save,
    ):
        await coordinator._async_prune_stale_metadata(set())

    assert "automation/hash_only.yaml" in coordinator._persisted_metadata
    assert (
        coordinator._persisted_metadata["automation/hash_only.yaml"]["remote_hash"] == "some_hash"
    )
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_async_prune_stale_metadata_triggers_save(coordinator):
    """Test that pruning stale metadata triggers a background save operation."""
    coordinator._persisted_metadata = {"automation/stale.yaml": {"remote_hash": "some_hash"}}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
        patch.object(coordinator, "_async_save_metadata") as mock_save,
    ):
        await coordinator._async_prune_stale_metadata(set())

    assert "automation/stale.yaml" not in coordinator._persisted_metadata
    mock_save.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_save_metadata_honors_cleared_in_memory_state(coordinator):
    """Test that clearing an ETag in-memory correctly results in it being removed from save."""
    path = "/config/blueprints/automation/test.yaml"
    coordinator._persisted_metadata = {
        "automation/test.yaml": {"etag": "old_etag", "remote_hash": "old_hash"}
    }

    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "source_url": None,
            "etag": None,
            "remote_hash": None,
        }
    }

    with (
        patch.object(coordinator._store, "async_save") as mock_save,
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
    ):
        await coordinator._async_save_metadata(force=True, skip_filter=True)

        mock_save.assert_called_once()
        save_data = mock_save.call_args[0][0]
        saved_metadata = save_data["metadata"]
        assert "automation/test.yaml" not in saved_metadata


@pytest.mark.asyncio
async def test_metadata_pruning(coordinator):
    """Test that stale metadata is pruned during update."""
    path_valid = "/config/blueprints/automation/valid.yaml"

    coordinator._persisted_metadata = {
        "automation/valid.yaml": {"etag": "etag1", "remote_hash": "hash1"},
        "automation/stale.yaml": {"etag": "etag2", "remote_hash": "hash2"},
    }

    blueprints = {
        path_valid: {
            "name": "Valid",
            "rel_path": "automation/valid.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": "hash1",
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        await coordinator._async_update_data()

    assert "automation/valid.yaml" in coordinator._persisted_metadata
    assert "automation/stale.yaml" not in coordinator._persisted_metadata


@pytest.mark.asyncio
async def test_async_save_metadata_empty_data(coordinator):
    """Test that saving metadata with empty data clears the store."""
    coordinator.data = {}
    coordinator.setup_complete = True
    coordinator._persisted_metadata = {"stale": {"etag": "etag", "remote_hash": "hash"}}

    with (
        patch.object(coordinator._store, "async_save", new_callable=AsyncMock) as mock_save,
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
    ):
        await coordinator._async_save_metadata()

    mock_save.assert_called_once_with({"metadata": {}})
    assert not coordinator._persisted_metadata


@pytest.mark.asyncio
async def test_prune_metadata_persistence(coordinator):
    """Test that stale metadata is pruned from memory and persisted to disk."""
    path_exist = "/config/blueprints/automation/exist.yaml"

    coordinator._persisted_metadata = {
        "automation/exist.yaml": {"etag": "e1", "remote_hash": "h1"},
        "automation/stale.yaml": {"etag": "e2", "remote_hash": "h2"},
    }
    coordinator.setup_complete = True

    coordinator.data = {
        path_exist: {"rel_path": "automation/exist.yaml", "etag": "e1", "remote_hash": "h1"}
    }

    tasks = []

    def create_background_task(coro, name=None):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    with (
        patch.object(
            coordinator.hass, "async_create_background_task", side_effect=create_background_task
        ),
        patch.object(coordinator._store, "async_save", new_callable=AsyncMock) as mock_save,
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile",
            side_effect=lambda p: p.replace("\\", "/") in {path_exist},
        ),
    ):
        await coordinator._async_prune_stale_metadata({path_exist})

        if tasks:
            await asyncio.gather(*tasks)

    assert "automation/stale.yaml" not in coordinator._persisted_metadata
    assert "automation/exist.yaml" in coordinator._persisted_metadata
    assert mock_save.called, "async_save was not called"
    saved_data = mock_save.call_args[0][0]
    assert "automation/stale.yaml" not in saved_data["metadata"]
    assert saved_data["metadata"]["automation/exist.yaml"]["etag"] == "e1"


@pytest.mark.asyncio
async def test_cold_start_rehydration(coordinator):
    """Test that persisted hashes are used on reboot but verified later."""
    path = "/config/blueprints/automation/test.yaml"
    local_hash = "current_hash"
    coordinator.data = {}
    coordinator._persisted_metadata = {
        "automation/test.yaml": {"remote_hash": local_hash, "source_url": "https://url"}
    }
    coordinator._first_update_done = False

    blueprints = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": local_hash,
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        results = await coordinator._async_update_data()

    assert not results[path]["updatable"]
    assert results[path]["remote_hash"] == local_hash
    assert coordinator._first_update_done


@pytest.mark.asyncio
async def test_etag_invalidation_on_mismatch(coordinator):
    """Test that ETag is invalidated when local and remote hashes mismatch on startup."""
    path = "/config/blueprints/automation/test.yaml"
    local_hash = "current_hash"
    remote_hash = "stale_hash"
    coordinator.data = {}
    coordinator._persisted_metadata = {
        "automation/test.yaml": {"remote_hash": remote_hash, "etag": "stale_etag"}
    }
    coordinator._first_update_done = False

    blueprints = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": local_hash,
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        results = await coordinator._async_update_data()

    assert results[path]["updatable"]
    assert results[path]["etag"] is None
    assert results[path]["remote_hash"] == remote_hash


@pytest.mark.asyncio
async def test_persisted_metadata_not_reused_after_first_update(coordinator):
    """Test that persisted hashes/ETags are only used for the very first update."""
    path = "/config/blueprints/automation/test.yaml"
    initial_hash = "initial_hash"
    coordinator._persisted_metadata = {
        "automation/test.yaml": {
            "remote_hash": initial_hash,
            "etag": "initial_etag",
            "source_url": "https://url",
        }
    }
    coordinator._first_update_done = False

    blueprints = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": initial_hash,
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        first_results = await coordinator._async_update_data()
    coordinator.data = first_results
    assert coordinator._first_update_done

    coordinator._persisted_metadata["automation/test.yaml"] = {
        "remote_hash": "stale_hash",
        "etag": "stale_etag",
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        results = await coordinator._async_update_data()

    assert results[path]["remote_hash"] == initial_hash
    assert results[path]["etag"] == "initial_etag"


@pytest.mark.asyncio
async def test_metadata_preservation_during_scan(coordinator):
    """Test that existing metadata is preserved during a scan until refreshed."""
    path = "/config/blueprints/automation/test.yaml"
    local_hash = "some_hash"
    coordinator.data = {
        path: {
            "local_hash": local_hash,
            "remote_hash": "remote_hash",
            "remote_content": "remote_content",
            "updatable": False,
            "invalid_remote_hash": "stale_error",
            "last_error": "failed_previously",
            "etag": "some_etag",
        }
    }

    blueprints = {
        path: {
            "name": "Test",
            "rel_path": "automation/test.yaml",
            "domain": "automation",
            "source_url": "https://url",
            "local_hash": local_hash,
        }
    }

    with (
        patch.object(coordinator, "scan_blueprints", return_value=blueprints),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        results = await coordinator._async_update_data()

    assert results[path]["invalid_remote_hash"] == "stale_error"
    assert results[path]["last_error"] == "failed_previously"
    assert results[path]["etag"] == "some_etag"
    assert results[path]["remote_content"] == "remote_content"


@pytest.mark.asyncio
async def test_backup_max_limit(coordinator, tmp_path):
    """Test that backups exceeding max_backups are cleaned up."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("v0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 2})
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    for i in range(1, 5):
        await coordinator.async_install_blueprint(
            str(bp_file), f"v{i}", reload_services=False, backup=True
        )

    assert bp_file.read_text() == "v4"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "v3"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "v2"
    assert not (tmp_path / "test.yaml.bak.3").exists()


@pytest.mark.asyncio
async def test_backup_rotation(coordinator, tmp_path):
    """Test that backups rotate correctly: .bak.1 is newest, .bak.3 is oldest."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("version_0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = MappingProxyType({"max_backups": 3})
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    for i in range(1, 4):
        await coordinator.async_install_blueprint(
            str(bp_file), f"version_{i}", reload_services=False, backup=True
        )

    assert bp_file.read_text() == "version_3"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "version_2"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "version_1"
    assert (tmp_path / "test.yaml.bak.3").read_text() == "version_0"


@pytest.mark.asyncio
async def test_async_restore_blueprint_error(hass, coordinator):
    """Test error handling during blueprint restoration."""
    path = "/config/blueprints/automation/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
        patch("custom_components.blueprints_updater.coordinator.os.rename"),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2"),
        patch(
            "custom_components.blueprints_updater.coordinator.os.replace",
            side_effect=Exception("Disk error"),
        ),
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "system_error"
    assert "Disk error" in result["translation_kwargs"]["error"]


@pytest.mark.asyncio
async def test_async_restore_blueprint_missing(hass, coordinator):
    """Test restoration when backup is missing."""
    path = "/config/blueprints/automation/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
        ),
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "missing_backup"


@pytest.mark.asyncio
async def test_async_restore_blueprint_success(hass, coordinator):
    """Test successful restoration of a blueprint backup."""
    path = "/config/blueprints/automation/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch(
            "custom_components.blueprints_updater.coordinator.os.path.realpath",
            side_effect=os.path.normpath,
        ),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
        patch("custom_components.blueprints_updater.coordinator.os.rename"),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2"),
    ):
        result = await coordinator.async_restore_blueprint(path)

    mock_replace.assert_any_call(
        os.path.normpath(f"{path}.bak.1"), os.path.normpath(f"{path}.bak.2")
    )
    assert result["success"] is True
    assert result["translation_key"] == "success"
    hass.services.async_call.assert_any_call("automation", "reload")


@pytest.mark.asyncio
async def test_async_restore_blueprint_unsafe_path(coordinator):
    """Test that restoring to an unsafe path is blocked."""
    coordinator._is_safe_path = BlueprintUpdateCoordinator._is_safe_path.__get__(coordinator)
    result = await coordinator.async_restore_blueprint("/config/secrets.yaml")
    assert result["success"] is False
    assert result["translation_key"] == "system_error"


@pytest.mark.asyncio
async def test_restore_versioned(coordinator, tmp_path):
    """Test restoring from a specific backup version."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("current")
    (tmp_path / "test.yaml.bak.1").write_text("backup_v1")
    (tmp_path / "test.yaml.bak.2").write_text("backup_v2")

    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    coordinator.async_reload_services = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    result = await coordinator.async_restore_blueprint(str(bp_file), version=2)
    assert result["success"] is True
    assert bp_file.read_text() == "backup_v2"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "backup_v1"
