"""Tests for coordinator error handling and edge cases."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.exceptions import HomeAssistantError


@pytest.mark.asyncio
async def test_metadata_persistence_failures(coordinator):
    """Test failures during metadata persistence."""
    coordinator.setup_complete = True
    coordinator.data = {"p1": {"rel_path": "a.yaml", "etag": "e"}}

    mock_store = MagicMock()
    coordinator._store = mock_store

    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=False
    ):
        await coordinator._async_save_metadata()
        mock_store.async_save.assert_not_called()

    mock_store.async_save = AsyncMock(side_effect=Exception("Disk full"))
    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch("custom_components.blueprints_updater.coordinator._LOGGER.exception") as mock_error,
    ):
        await coordinator._async_save_metadata(force=True)
        assert any(
            "Failed to save metadata to storage" in str(call) for call in mock_error.call_args_list
        )


@pytest.mark.asyncio
async def test_background_refresh_error_scenarios(coordinator):
    """Test background refresh resilience to errors."""
    blueprints = {"path1": {"source_url": "url1", "rel_path": "a.yaml"}}

    coordinator._async_update_blueprint_in_place = AsyncMock(
        side_effect=RuntimeError("Worker Boom")
    )

    with patch("custom_components.blueprints_updater.coordinator._LOGGER.exception") as mock_exc:
        await coordinator._async_background_refresh(blueprints)
        assert any(
            "Error in background worker for %s" in str(call) for call in mock_exc.call_args_list
        )


@pytest.mark.asyncio
async def test_blueprint_installation_security_and_errors(coordinator):
    """Test security checks and error handling during installation."""
    path = "/config/blueprints/automation/test.yaml"

    with (
        patch.object(coordinator, "_is_safe_path", return_value=False),
        pytest.raises(HomeAssistantError, match="Security violation"),
    ):
        await coordinator.async_install_blueprint(path, "content")

    coordinator.config_entry.options = {"max_backups": 1}
    with (
        patch.object(coordinator, "_is_safe_path", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.shutil.copy2",
            side_effect=OSError("Permission denied"),
        ),
        patch("custom_components.blueprints_updater.coordinator._LOGGER.warning") as mock_warn,
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
    ):
        await coordinator.async_install_blueprint(path, "content", backup=True)
        assert any(
            "Permission denied" in str(arg)
            for call in mock_warn.call_args_list
            for arg in call.args
        )


@pytest.mark.asyncio
async def test_async_fetch_content_failures(coordinator):
    """Test failures during remote content fetching."""
    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=httpx.RequestError("Network Down"))

    with (
        patch("custom_components.blueprints_updater.coordinator._LOGGER.debug"),
        pytest.raises(httpx.RequestError),
    ):
        await coordinator._async_fetch_content(mock_session, "url", "etag")


@pytest.mark.asyncio
async def test_notification_handling(coordinator):
    """Test persistent notification creation."""
    coordinator.hass.services.async_call = AsyncMock()
    coordinator.async_translate = AsyncMock(return_value="translated")

    await coordinator._async_handle_notifications(["BP1", "BP2"], {"automation"})
    coordinator.hass.services.async_call.assert_any_call("persistent_notification", "create", ANY)


@pytest.mark.asyncio
async def test_invalidate_metadata(coordinator):
    """Test metadata invalidation."""
    path = "automation/test.yaml"
    coordinator._persisted_metadata = {path: {"etag": "e"}}
    prev = {"rel_path": path, "etag": "e", "source_url": "old"}
    coordinator.data = {path: prev}
    coordinator._invalidate_blueprint_metadata(path, "old", "new", prev)
    assert path not in coordinator._persisted_metadata
    assert coordinator.data[path]["etag"] is None


@pytest.mark.asyncio
async def test_prune_stale_metadata_exception(coordinator):
    """Test _async_prune_stale_metadata handles path errors."""
    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.relpath",
        side_effect=ValueError("Bad path"),
    ):
        await coordinator._async_prune_stale_metadata(["/some/path"])


@pytest.mark.asyncio
async def test_async_update_data_scan_failure(coordinator):
    """Test _async_update_data handles scanning failures gracefully."""
    with (
        patch.object(coordinator, "scan_blueprints", side_effect=Exception("Scan Crash")),
        patch("custom_components.blueprints_updater.coordinator._LOGGER.exception") as mock_error,
    ):
        with pytest.raises(HomeAssistantError, match="Blueprint scan failed: Scan Crash"):
            await coordinator._async_update_data()
        assert any("Blueprint scan failed" in str(call) for call in mock_error.call_args_list)
