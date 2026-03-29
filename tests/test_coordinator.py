import os
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import (
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(
            hass,
            entry,
            timedelta(hours=24),
        )

        coord._listeners = cast(Any, {})
        coord.hass = hass
        coord.data = {}

        def _mock_set_data(data):
            coord.data = data

        coord.async_set_updated_data = cast(Any, MagicMock(side_effect=_mock_set_data))
        coord.async_update_listeners = cast(Any, MagicMock())
        return coord


def test_normalize_url(coordinator):
    """Test URL normalization."""
    assert (
        coordinator._normalize_url("https://github.com/user/repo/blob/main/blueprints/test.yaml")
        == "https://raw.githubusercontent.com/user/repo/main/blueprints/test.yaml"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://gist.github.com/user/gist_id/raw")
        == "https://gist.github.com/user/gist_id/raw"
    )

    assert (
        coordinator._normalize_url("https://community.home-assistant.io/t/topic-slug/12345")
        == "https://community.home-assistant.io/t/12345.json"
    )

    assert (
        coordinator._normalize_url("https://example.com/blueprint.yaml")
        == "https://example.com/blueprint.yaml"
    )


def test_parse_forum_content(coordinator):
    """Test parsing forum content."""
    json_data = {
        "post_stream": {
            "posts": [
                {
                    "cooked": (
                        '<p>Here is my blueprint:</p><pre><code class="lang-yaml">blueprint:\n'
                        "  name: Test\n  source_url: https://url.com</code></pre>"
                    )
                }
            ]
        }
    }
    content = coordinator._parse_forum_content(json_data)
    assert "blueprint:" in content
    assert "name: Test" in content

    json_data_no_bp = {"post_stream": {"posts": [{"cooked": "<code>not a blueprint</code>"}]}}
    assert coordinator._parse_forum_content(json_data_no_bp) is None

    assert coordinator._parse_forum_content({}) is None
    assert coordinator._parse_forum_content({"post_stream": {"posts": []}}) is None


def test_ensure_source_url(coordinator):
    """Test ensuring source_url is present."""
    source_url = "https://github.com/user/repo/blob/main/test.yaml"

    content = "blueprint:\n  name: Test"
    new_content = coordinator._ensure_source_url(content, source_url)
    assert f"source_url: {source_url}" in new_content

    content_with_url = f"blueprint:\n  name: Test\n  source_url: {source_url}"
    assert coordinator._ensure_source_url(content_with_url, source_url) == content_with_url

    content_with_quotes = f"blueprint:\n  name: Test\n  source_url: '{source_url}'"
    assert coordinator._ensure_source_url(content_with_quotes, source_url) == content_with_quotes


def test_scan_blueprints(hass, coordinator):
    """Test scanning blueprints directory."""
    bp_path = "/config/blueprints"
    mock_files = [(bp_path, [], ["valid.yaml", "invalid.yaml", "no_url.yaml", "not_yaml.txt"])]

    valid_content = "blueprint:\n  name: Valid\n  source_url: https://url.com"
    invalid_content = "not: a blueprint"
    no_url_content = "blueprint:\n  name: No URL"

    def open_side_effect(path, *_args, **_kwargs):
        path_str = str(path)
        basename = os.path.basename(path_str)
        content = ""
        if basename == "valid.yaml":
            content = valid_content
        elif basename == "invalid.yaml":
            content = invalid_content
        elif basename == "no_url.yaml":
            content = no_url_content

        m = MagicMock()
        m.read.return_value = content
        m.__enter__.return_value = m
        return m

    with (
        patch("os.path.isdir", return_value=True),
        patch("os.walk", return_value=mock_files),
        patch("builtins.open", side_effect=open_side_effect),
    ):
        results = coordinator.scan_blueprints(hass, FILTER_MODE_ALL, [])
        assert len(results) == 1, f"Expected 1, got {len(results)}: {results.keys()}"
        assert any("valid.yaml" in k for k in results)
        full_path = next(iter(results.keys()))
        assert results[full_path]["rel_path"] == "valid.yaml"

        results = coordinator.scan_blueprints(hass, FILTER_MODE_WHITELIST, ["valid.yaml"])
        assert len(results) == 1

        results = coordinator.scan_blueprints(hass, FILTER_MODE_WHITELIST, ["other.yaml"])
        assert len(results) == 0

        results = coordinator.scan_blueprints(hass, FILTER_MODE_BLACKLIST, ["valid.yaml"])
        assert len(results) == 0


@pytest.mark.asyncio
async def test_async_update_blueprint(coordinator):
    """Test the full update flow for a single blueprint."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://github.com/user/repo/blob/main/test.yaml",
        "hash": "old_hash",
    }
    results = {path: {"last_error": None, "hash": "old_hash"}}

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.headers = {"ETag": "new_etag"}
    mock_response.raise_for_status = MagicMock()
    mock_response.text = AsyncMock(return_value="blueprint:\n  name: Test")

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response

    with (
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
    ):
        mock_hash.return_value.hexdigest.return_value = "new_hash"
        coordinator.data = results
        results_to_notify = []
        await coordinator._async_update_blueprint_in_place(
            mock_session, path, info, results_to_notify
        )

    assert path in results
    assert results[path]["updatable"] is True
    assert results[path]["remote_hash"] == "new_hash"
    assert results[path]["etag"] == "new_etag"
    assert "source_url" in results[path]["remote_content"]


@pytest.mark.asyncio
async def test_async_update_blueprint_not_modified(coordinator):
    """Test the update flow when server returns 304 Not Modified."""
    path = "/config/blueprints/test.yaml"
    info = {
        "name": "Test",
        "rel_path": "test.yaml",
        "source_url": "https://url",
        "hash": "old_hash",
    }
    coordinator.data = {
        path: {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": "https://url",
            "local_hash": "old_hash",
            "updatable": False,
            "remote_hash": "old_hash",
            "etag": "old_etag",
        }
    }

    mock_response = MagicMock()
    mock_response.status = 304
    mock_response.headers = {"ETag": "old_etag"}
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response

    results_to_notify = []
    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)

    assert coordinator.data[path]["etag"] == "old_etag"
    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path].get("remote_content") is None


@pytest.mark.asyncio
async def test_async_install_blueprint(hass, coordinator):
    """Test installing a blueprint and reloading services."""
    path = "/config/blueprints/test.yaml"
    remote_content = "blueprint:\n  name: Test"

    hass.services.has_service = MagicMock(
        side_effect=lambda domain, service: (
            domain in ["automation", "script"] if service == "reload" else False
        )
    )
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace"),
        patch("custom_components.blueprints_updater.coordinator.os.path.isfile", return_value=True),
    ):
        await coordinator.async_install_blueprint(path, remote_content)

    assert hass.services.async_call.call_count == 2
    hass.services.async_call.assert_any_call("automation", "reload")
    hass.services.async_call.assert_any_call("script", "reload")

    with pytest.raises(AssertionError):
        hass.services.async_call.assert_any_call("template", "reload")


@pytest.mark.asyncio
async def test_async_update_data_partial_failure(coordinator):
    """Test that one failed blueprint does not stop others."""
    blueprints = {
        "/config/blueprints/good.yaml": {
            "name": "Good",
            "rel_path": "good.yaml",
            "source_url": "https://url.com/good.yaml",
            "hash": "good_hash",
        },
        "/config/blueprints/bad.yaml": {
            "name": "Bad",
            "rel_path": "bad.yaml",
            "source_url": "https://url.com/bad.yaml",
            "hash": "bad_hash",
        },
    }

    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    mock_good_resp = MagicMock()
    mock_good_resp.status = 200
    mock_good_resp.headers = {"ETag": "good_etag"}
    mock_good_resp.raise_for_status = MagicMock()
    mock_good_resp.text = AsyncMock(return_value="blueprint:\n  name: Good")

    mock_bad_resp = MagicMock()
    mock_bad_resp.status = 404
    mock_bad_resp.headers = {}
    mock_bad_resp.raise_for_status = MagicMock(side_effect=Exception("404 Not Found"))

    @patch("custom_components.blueprints_updater.coordinator.async_get_clientsession")
    async def run_test(mock_get_session):
        mock_session = MagicMock()
        mock_get_session.return_value = mock_session

        def get_side_effect(url, **_kwargs):
            m = MagicMock()
            m.__aenter__ = AsyncMock()
            if "good.yaml" in url:
                m.__aenter__.return_value = mock_good_resp
            else:
                m.__aenter__.return_value = mock_bad_resp
            return m

        mock_session.get.side_effect = get_side_effect

        with (
            patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
            patch.object(coordinator, "_validate_blueprint", return_value=None),
        ):
            mock_hash.return_value.hexdigest.return_value = "new_hash"
            with patch.object(coordinator, "_start_background_refresh"):
                update_results = await coordinator._async_update_data()
                coordinator.data = update_results
                await coordinator._async_background_refresh(blueprints)
            return update_results

    results = await run_test()

    assert "/config/blueprints/good.yaml" in results
    assert "/config/blueprints/bad.yaml" in results

    assert results["/config/blueprints/good.yaml"]["updatable"] is True
    assert results["/config/blueprints/good.yaml"]["last_error"] is None

    assert results["/config/blueprints/bad.yaml"]["last_error"] is not None
    assert "404" in results["/config/blueprints/bad.yaml"]["last_error"]


@pytest.mark.asyncio
async def test_async_update_blueprint_in_place_errors(coordinator):
    """Test various error conditions in _async_update_blueprint_in_place."""
    path = "/config/blueprints/test.yaml"
    info = {"name": "Test", "source_url": "https://url", "hash": "hash"}
    results = {
        path: {
            "last_error": None,
            "hash": "hash",
            "name": "Test",
            "source_url": "https://url",
        }
    }
    coordinator.data = results

    mock_resp_empty = MagicMock()
    mock_resp_empty.status = 200
    mock_resp_empty.headers = {}
    mock_resp_empty.raise_for_status = MagicMock()
    mock_resp_empty.text = AsyncMock(return_value="")

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_resp_empty
    results_to_notify = []

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert coordinator.data[path]["last_error"] == "empty_content"

    mock_resp_invalid = MagicMock()
    mock_resp_invalid.status = 200
    mock_resp_invalid.headers = {}
    mock_resp_invalid.raise_for_status = MagicMock()
    mock_resp_invalid.text = AsyncMock(return_value="}invalid yaml: {\n")
    mock_session.get.return_value.__aenter__.return_value = mock_resp_invalid

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "yaml_syntax_error" in str(coordinator.data[path]["last_error"])

    mock_resp_missing_bp = MagicMock()
    mock_resp_missing_bp.status = 200
    mock_resp_missing_bp.headers = {}
    mock_resp_missing_bp.raise_for_status = MagicMock()
    mock_resp_missing_bp.text = AsyncMock(return_value="other_key: value\nsource_url: https://url")
    mock_session.get.return_value.__aenter__.return_value = mock_resp_missing_bp

    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "invalid_blueprint" in str(coordinator.data[path]["last_error"])

    mock_session.get.side_effect = Exception("Connection Failed")
    await coordinator._async_update_blueprint_in_place(mock_session, path, info, results_to_notify)
    assert "fetch_error" in str(coordinator.data[path]["last_error"])
    assert "Connection Failed" in str(coordinator.data[path]["last_error"])


@pytest.mark.asyncio
async def test_async_install_blueprint_error(coordinator):
    """Test exception during blueprint installation."""
    with (
        patch("builtins.open", side_effect=Exception("Write failed")),
        pytest.raises(Exception, match="Write failed"),
    ):
        await coordinator.async_install_blueprint("/fake/path.yaml", "content")


@pytest.mark.asyncio
async def test_async_update_data_auto_update(coordinator):
    """Test _async_update_data with auto_update enabled."""
    coordinator.config_entry.options = {"auto_update": True}
    blueprints = {
        "/test.yaml": {
            "name": "Test",
            "rel_path": "test.yaml",
            "source_url": "https://url",
            "hash": "old",
        }
    }
    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"ETag": "new"}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value="blueprint:\n  name: Test\n  source_url: https://url")

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "async_install_blueprint") as mock_install,
        patch.object(coordinator, "async_reload_services") as mock_reload,
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_translations",
            return_value={
                "component.blueprints_updater.common.auto_update_title": "Title",
                "component.blueprints_updater.common.auto_update_message": "Msg {blueprints}",
            },
        ),
    ):
        mock_session.__aenter__.return_value = mock_session
        mock_hash.return_value.hexdigest.return_value = "new"

        with patch.object(coordinator, "_start_background_refresh"):
            results = await coordinator._async_update_data()
            coordinator.data = results
            await coordinator._async_background_refresh(blueprints)

        mock_install.assert_called_once_with(
            "/test.yaml",
            "blueprint:\n  name: Test\n  source_url: https://url",
            reload_services=False,
            backup=True,
        )
        mock_reload.assert_called_once()

        coordinator.hass.services.async_call.assert_any_call(
            "persistent_notification",
            "create",
            {
                "title": "Title",
                "message": "Msg - Test",
                "notification_id": "blueprints_updater_auto_update",
            },
        )

        assert "/test.yaml" in coordinator.data
        assert coordinator.data["/test.yaml"]["updatable"] is False
        assert coordinator.data["/test.yaml"]["remote_content"] is None
        assert coordinator.data["/test.yaml"]["local_hash"] == "new"
        assert coordinator.data["/test.yaml"]["etag"] == "new"


@pytest.mark.asyncio
async def test_async_update_data_auto_update_multiple_sorted(coordinator):
    """Test _async_update_data sorts multiple auto-updated blueprints."""
    coordinator.config_entry.options = {"auto_update": True}
    blueprints = {
        "/b.yaml": {
            "name": "Beta",
            "rel_path": "b.yaml",
            "source_url": "https://url/b",
            "hash": "old",
        },
        "/a.yaml": {
            "name": "Alpha",
            "rel_path": "a.yaml",
            "source_url": "https://url/a",
            "hash": "old",
        },
    }
    coordinator.scan_blueprints = MagicMock(return_value=blueprints)

    mock_resp_a = MagicMock()
    mock_resp_a.status = 200
    mock_resp_a.headers = {"ETag": "new"}
    mock_resp_a.raise_for_status = MagicMock()
    mock_resp_a.text = AsyncMock(
        return_value="blueprint:\n  name: Alpha\n  source_url: https://url/a"
    )

    mock_resp_b = MagicMock()
    mock_resp_b.status = 200
    mock_resp_b.headers = {"ETag": "new"}
    mock_resp_b.raise_for_status = MagicMock()
    mock_resp_b.text = AsyncMock(
        return_value="blueprint:\n  name: Beta\n  source_url: https://url/b"
    )

    mock_session = MagicMock()
    mock_session.__aenter__.return_value = mock_session

    def get_side_effect(url, **_kwargs):
        m = MagicMock()
        m.__aenter__.return_value = mock_resp_a if "/a" in url else mock_resp_b
        return m

    mock_session.get.side_effect = get_side_effect

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch("custom_components.blueprints_updater.coordinator.hashlib.sha256") as mock_hash,
        patch.object(coordinator, "async_install_blueprint"),
        patch.object(coordinator, "async_reload_services"),
        patch.object(coordinator, "_validate_blueprint", return_value=None),
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_translations",
            return_value={
                "component.blueprints_updater.common.auto_update_title": "Title",
                "component.blueprints_updater.common.auto_update_message": "Msg\n{blueprints}",
            },
        ),
    ):
        mock_hash.return_value.hexdigest.return_value = "new"

        with patch.object(coordinator, "_start_background_refresh"):
            results = await coordinator._async_update_data()
            coordinator.data = results
            await coordinator._async_background_refresh(blueprints)

        args = coordinator.hass.services.async_call.call_args_list
        notification_call = next(
            c for c in args if c.args[0] == "persistent_notification" and c.args[1] == "create"
        )
        message = notification_call.args[2]["message"]

        assert "Alpha" in message
        assert "Beta" in message
        assert message.index("Alpha") < message.index("Beta")
        assert message == "Msg\n- Alpha\n- Beta"


@pytest.mark.asyncio
async def test_async_install_blueprint_backup(hass, coordinator):
    """Test installing a blueprint with backup enabled."""
    path = "/config/blueprints/test.yaml"
    remote_content = "blueprint:\n  name: Test"

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = {"max_backups": 3}
    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()

    with (
        patch("builtins.open", MagicMock()),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.shutil.copy2") as mock_copy,
        patch("custom_components.blueprints_updater.coordinator.os.remove"),
    ):
        await coordinator.async_install_blueprint(path, remote_content, backup=True)

    mock_copy.assert_called_once_with(path, f"{path}.bak.1")
    mock_replace.assert_any_call(f"{path}.tmp", path)


@pytest.mark.asyncio
async def test_async_restore_blueprint_success(hass, coordinator):
    """Test successful restoration of a blueprint backup."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    hass.services.has_service = MagicMock(return_value=True)
    hass.services.async_call = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch("custom_components.blueprints_updater.coordinator.os.replace") as mock_replace,
    ):
        result = await coordinator.async_restore_blueprint(path)

    mock_replace.assert_called_once_with(f"{path}.bak.1", path)
    assert result["success"] is True
    assert result["translation_key"] == "success"
    hass.services.async_call.assert_any_call("automation", "reload")


@pytest.mark.asyncio
async def test_async_restore_blueprint_missing(hass, coordinator):
    """Test restoration when backup is missing."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with patch(
        "custom_components.blueprints_updater.coordinator.os.path.exists", return_value=False
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "missing_backup"


@pytest.mark.asyncio
async def test_async_restore_blueprint_error(hass, coordinator):
    """Test error handling during blueprint restoration."""
    path = "/config/blueprints/test.yaml"
    coordinator.data = {path: {"updatable": False}}

    with (
        patch("custom_components.blueprints_updater.coordinator.os.path.exists", return_value=True),
        patch(
            "custom_components.blueprints_updater.coordinator.os.replace",
            side_effect=Exception("Disk error"),
        ),
    ):
        result = await coordinator.async_restore_blueprint(path)

    assert result["success"] is False
    assert result["translation_key"] == "system_error"
    assert "Disk error" in result["translation_kwargs"]["error"]


def test_validate_blueprint_valid(coordinator):
    """Test _validate_blueprint with valid data returns None."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is None


def test_validate_blueprint_incompatible_version(coordinator):
    """Test _validate_blueprint blocks when min_version is too high."""
    data = {
        "blueprint": {
            "name": "Test",
            "domain": "automation",
            "input": {},
            "homeassistant": {"min_version": "2099.1.0"},
        },
        "trigger": [],
        "action": [],
    }
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "incompatible" in result
    assert "2099.1.0" in result


def test_validate_blueprint_schema_error(coordinator):
    """Test _validate_blueprint catches schema validation errors."""
    data = {"blueprint": {"name": "Test"}}
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint(data, "https://example.com/bp.yaml")
    assert result is not None
    assert "validation_error" in result


def test_validate_blueprint_missing_key(coordinator):
    """Test _validate_blueprint with data missing the 'blueprint' key."""
    coordinator.hass.data = {}
    result = coordinator._validate_blueprint({"not_blueprint": {}}, "https://example.com/bp.yaml")
    assert result is not None
    assert "invalid_blueprint" in result


@pytest.mark.asyncio
async def test_backup_rotation(coordinator, tmp_path):
    """Test that backups rotate correctly: .bak.1 is newest, .bak.3 is oldest."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("version_0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = {"max_backups": 3}
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
async def test_backup_max_limit(coordinator, tmp_path):
    """Test that backups exceeding max_backups are cleaned up."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("v0")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = {"max_backups": 2}
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
    assert not (tmp_path / "test.yaml.bak.2").exists()


@pytest.mark.asyncio
async def test_backup_migration_old_bak(coordinator, tmp_path):
    """Test migration of old .bak format to .bak.1 on next backup."""
    bp_file = tmp_path / "test.yaml"
    bp_file.write_text("current")
    (tmp_path / "test.yaml.bak").write_text("old_backup")

    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = {"max_backups": 3}
    coordinator.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    await coordinator.async_install_blueprint(
        str(bp_file), "new_version", reload_services=False, backup=True
    )

    assert bp_file.read_text() == "new_version"
    assert (tmp_path / "test.yaml.bak.1").read_text() == "current"
    assert (tmp_path / "test.yaml.bak.2").read_text() == "old_backup"
    assert not (tmp_path / "test.yaml.bak").exists()


@pytest.mark.asyncio
async def test_async_staggered_update_delays(coordinator):
    """Test that background refresh uses staggered delays."""
    blueprints = {
        f"/path/{i}.yaml": {
            "name": f"BP {i}",
            "rel_path": f"{i}.yaml",
            "source_url": f"https://url/{i}",
            "hash": "hash",
        }
        for i in range(3)
    }

    coordinator.scan_blueprints = MagicMock(return_value=blueprints)
    coordinator.data = {}
    mock_session = MagicMock()

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.async_get_clientsession",
            return_value=mock_session,
        ),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch.object(coordinator, "_async_update_blueprint_in_place", return_value=None),
        patch.object(coordinator, "_start_background_refresh"),
    ):
        results = await coordinator._async_update_data()
        coordinator.data = results
        await coordinator._async_background_refresh(blueprints)

    sleep_calls = [call.args[0] for call in mock_sleep.call_args_list]
    assert all(d == 1.0 for d in sleep_calls)
    assert len(sleep_calls) == 3
