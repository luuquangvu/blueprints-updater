"""Tests for coordinator URL-change handling, startup merging, and 304 not-modified cases."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.blueprints_updater.const import FilterMode
from custom_components.blueprints_updater.coordinator import (
    BlueprintScanContext,
    BlueprintUpdateCoordinator,
)


@pytest.fixture
def mock_makedirs() -> Generator[MagicMock]:
    """Mock os.makedirs to prevent creating actual directories on disk."""
    with patch("os.makedirs") as mock:
        yield mock


def test_hash_content_stability_across_forum_slug_variations() -> None:
    """Test that _hash_content produces deterministic hashes regardless of forum slug changes."""
    content = """blueprint:
  name: Test Blueprint
  domain: automation
  input: {}
trigger:
  - platform: state
    entity_id: input_boolean.test
action:
  - service: light.toggle
"""
    url_slug_a = "https://community.home-assistant.io/t/initial-title-slug/787779"
    url_slug_b = "https://community.home-assistant.io/t/updated-title-slug-v1-1/787779"
    url_short = "https://community.home-assistant.io/t/787779"

    hash_a = BlueprintUpdateCoordinator._hash_content(content, url_slug_a)
    hash_b = BlueprintUpdateCoordinator._hash_content(content, url_slug_b)
    hash_short = BlueprintUpdateCoordinator._hash_content(content, url_short)

    assert hash_a == hash_b
    assert hash_a == hash_short


def test_hash_content_stability_across_github_blob_and_raw_urls() -> None:
    """Test that _hash_content produces the same hash for GitHub blob and raw URL forms.

    A blueprint stored via a github.com/blob/ URL and the equivalent
    raw.githubusercontent.com URL represent the same resource; the content
    hash must be identical for update-detection to work correctly.
    """
    content = """blueprint:
  name: Test Blueprint
  domain: automation
  input: {}
trigger:
  - platform: state
    entity_id: input_boolean.test
action:
  - service: light.toggle
"""
    blob_url = "https://github.com/owner/repo/blob/main/blueprints/test.yaml"
    raw_url = "https://raw.githubusercontent.com/owner/repo/main/blueprints/test.yaml"

    hash_blob = BlueprintUpdateCoordinator._hash_content(content, blob_url)
    hash_raw = BlueprintUpdateCoordinator._hash_content(content, raw_url)

    assert hash_blob == hash_raw


def test_ensure_source_url_canonical_normalization() -> None:
    """Test _ensure_source_url embeds the original URL into blueprint YAML.

    Hashing remains canonical and identity-aware.
    """
    raw_content = """blueprint:
  name: Forum Blueprint
  domain: automation
  source_url: https://community.home-assistant.io/t/old-slug/787779
  input: {}
"""
    slug_url = "https://community.home-assistant.io/t/new-slug-v1-1/787779"
    ensured = BlueprintUpdateCoordinator._ensure_source_url(raw_content, slug_url)

    assert f"source_url: {slug_url}" in ensured
    hash_old = BlueprintUpdateCoordinator._hash_content(
        raw_content, "https://community.home-assistant.io/t/old-slug/787779"
    )
    hash_new = BlueprintUpdateCoordinator._hash_content(raw_content, slug_url)
    assert hash_old == hash_new


@pytest.mark.asyncio
async def test_handle_source_url_change_preserves_equivalent_forum_urls(
    coordinator: BlueprintUpdateCoordinator, mock_makedirs: MagicMock
) -> None:
    """Test that _handle_source_url_change preserves all metadata on topic slug changes."""
    path = "automation/smart_knob.yaml"
    prev_url = "https://community.home-assistant.io/t/old-slug/787779"
    curr_url = "https://community.home-assistant.io/t/new-slug-v1-1/787779"

    coordinator._persisted_metadata = {
        path: {
            "etag": "etag_123",
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "remote_hash": "hash_123",
            "source_url": prev_url,
        }
    }
    prev_info: dict[str, object] = {
        "relative_path": path,
        "source_url": prev_url,
        "etag": "etag_123",
        "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
        "remote_hash": "hash_123",
    }
    curr_info: dict[str, object] = {
        "relative_path": path,
        "source_url": curr_url,
        "local_hash": "local_123",
    }

    result = coordinator._handle_source_url_change(path, curr_info, prev_info, prev_url=prev_url)

    # Metadata should NOT be invalidated because topic IDs are identical
    assert result.get("etag") == "etag_123"
    assert result.get("last_modified") == "Mon, 01 Jan 2026 00:00:00 GMT"
    assert result.get("remote_hash") == "hash_123"
    assert path in coordinator._persisted_metadata


@pytest.mark.asyncio
async def test_handle_source_url_change_invalidates_on_different_source(
    coordinator: BlueprintUpdateCoordinator, mock_makedirs: MagicMock
) -> None:
    """Test that _handle_source_url_change DOES invalidate metadata on genuine URL change."""
    path = "automation/smart_knob.yaml"
    prev_url = "https://community.home-assistant.io/t/topic-a/11111"
    curr_url = "https://community.home-assistant.io/t/topic-b/22222"

    coordinator._persisted_metadata = {
        path: {
            "etag": "etag_123",
            "remote_hash": "hash_123",
            "source_url": prev_url,
        }
    }
    prev_info: dict[str, object] = {
        "relative_path": path,
        "source_url": prev_url,
        "etag": "etag_123",
        "remote_hash": "hash_123",
    }
    curr_info: dict[str, object] = {
        "relative_path": path,
        "source_url": curr_url,
        "local_hash": "local_123",
    }

    result = coordinator._handle_source_url_change(path, curr_info, prev_info, prev_url=prev_url)

    # Metadata SHOULD be cleared because topic IDs differ
    assert result.get("etag") is None
    assert result.get("remote_hash") is None
    assert path not in coordinator._persisted_metadata


def test_merge_previous_data_startup_matching_and_mismatching(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that startup _merge_previous_data preserves ETags on match and flags on mismatch."""
    coordinator.data = {}
    coordinator._persisted_metadata = {
        "automation/match.yaml": {
            "etag": "saved_etag_111",
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "remote_hash": "matching_hash_111",
            "source_url": "https://example.com/match.yaml",
        },
        "automation/mismatch.yaml": {
            "etag": "saved_etag_222",
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "remote_hash": "stale_hash_222",
            "source_url": "https://example.com/mismatch.yaml",
        },
    }

    results: dict[str, dict[str, object]] = {
        "automation/match.yaml": {
            "name": "Match BP",
            "relative_path": "automation/match.yaml",
            "domain": "automation",
            "source_url": "https://example.com/match.yaml",
            "local_hash": "matching_hash_111",
            "remote_hash": "matching_hash_111",
            "etag": "saved_etag_111",
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "persisted_source_url": "https://example.com/match.yaml",
            "updatable": False,
        },
        "automation/mismatch.yaml": {
            "name": "Mismatch BP",
            "relative_path": "automation/mismatch.yaml",
            "domain": "automation",
            "source_url": "https://example.com/mismatch.yaml",
            "local_hash": "new_local_hash_333",
            "remote_hash": "stale_hash_222",
            "etag": "saved_etag_222",
            "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "persisted_source_url": "https://example.com/mismatch.yaml",
            "updatable": False,
        },
    }

    coordinator._merge_previous_data(results)

    match_info = results["automation/match.yaml"]
    assert match_info["updatable"] is False
    assert match_info["etag"] == "saved_etag_111"
    assert match_info["last_modified"] == "Mon, 01 Jan 2026 00:00:00 GMT"

    mismatch_info = results["automation/mismatch.yaml"]
    assert mismatch_info["updatable"] is True
    assert mismatch_info["etag"] is None
    assert mismatch_info["last_modified"] is None


@pytest.mark.asyncio
async def test_handle_not_modified_case_matching_hashes_marks_not_updatable(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that 304 with matching hashes transitions updatable from True to False."""
    path = "automation/bp.yaml"
    coordinator.data = {
        path: {
            "name": "Test BP",
            "relative_path": path,
            "domain": "automation",
            "source_url": "https://example.com/bp.yaml",
            "local_hash": "same_hash_123",
            "remote_hash": "same_hash_123",
            "updatable": True,
        }
    }

    info: dict[str, object] = {
        "name": "Test BP",
        "local_hash": "same_hash_123",
    }

    session = MagicMock()
    await coordinator._handle_not_modified_case(
        session=session,
        path=path,
        info=info,
        normalized_url="https://example.com/bp.yaml",
        new_etag="new_etag_789",
    )

    assert coordinator.data[path]["updatable"] is False
    assert coordinator.data[path]["etag"] == "new_etag_789"


@pytest.mark.asyncio
async def test_handle_not_modified_case_mismatching_hashes_marks_updatable(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that 304 with mismatching hashes transitions updatable to True."""
    path = "automation/bp.yaml"
    coordinator.data = {
        path: {
            "name": "Test BP",
            "relative_path": path,
            "domain": "automation",
            "source_url": "https://example.com/bp.yaml",
            "local_hash": "local_hash_123",
            "remote_hash": "remote_hash_456",
            "updatable": False,
        }
    }

    info: dict[str, object] = {
        "name": "Test BP",
        "local_hash": "local_hash_123",
    }

    session = MagicMock()
    await coordinator._handle_not_modified_case(
        session=session,
        path=path,
        info=info,
        normalized_url="https://example.com/bp.yaml",
        new_etag="new_etag_789",
    )

    assert coordinator.data[path]["updatable"] is True
    assert coordinator.data[path]["etag"] == "new_etag_789"


@pytest.mark.asyncio
async def test_handle_not_modified_case_missing_remote_hash(
    coordinator: BlueprintUpdateCoordinator,
) -> None:
    """Test that 304 handles missing remote_hash safely."""
    path = "automation/bp.yaml"
    coordinator.data = {
        path: {
            "name": "Test BP",
            "relative_path": path,
            "domain": "automation",
            "source_url": "https://example.com/bp.yaml",
            "local_hash": "local_hash_123",
            "remote_hash": None,
            "updatable": False,
        }
    }

    info: dict[str, object] = {
        "name": "Test BP",
        "local_hash": "local_hash_123",
    }

    session = MagicMock()
    result = await coordinator._handle_not_modified_case(
        session=session,
        path=path,
        info=info,
        normalized_url="https://example.com/bp.yaml",
        new_etag="new_etag_789",
    )

    assert result == (None, "new_etag_789", None)
    assert coordinator.data[path]["updatable"] is False


@pytest.mark.parametrize(
    "malformed_source_url",
    [
        "https://example.com:invalid_port/bp.yaml",
        "https://example.com:70000/bp.yaml",
        "https://[::1/path",
        "https://[2001:db8::1/path",
        "https:///blueprint.yaml",
        "https://:443/blueprint.yaml",
        "//host/blueprint.yaml",
        "blueprint.yaml",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/bp.yaml",
        "gopher://example.com/test",
    ],
)
def test_parse_blueprint_data_rejects_malformed_port(malformed_source_url: str) -> None:
    """_parse_blueprint_data must reject blueprints with malformed or unusable source_url."""
    content = f"""blueprint:
  name: Malformed Port Blueprint
  domain: automation
  source_url: {malformed_source_url}
  input: {{}}
"""
    result = BlueprintUpdateCoordinator._parse_blueprint_data("automation/test.yaml", content)
    assert result is None


def test_parse_blueprint_data_stores_original_source_url() -> None:
    """_parse_blueprint_data must store the clean original source_url in blueprint metadata."""
    content = """blueprint:
  name: Forum Blueprint
  domain: automation
  source_url: HTTPS://community.home-assistant.io:443/t/some-slug/787779
  input: {}
"""
    result = BlueprintUpdateCoordinator._parse_blueprint_data("automation/test.yaml", content)
    assert result is not None
    assert result["source_url"] == "HTTPS://community.home-assistant.io:443/t/some-slug/787779"


@pytest.mark.parametrize(
    "malformed_source_url",
    [
        "https://example.com:invalid_port/bp.yaml",
        "https://example.com:70000/bp.yaml",
        "https://[::1/path",
        "https://[2001:db8::1/path",
    ],
)
def test_hash_content_handles_malformed_port_gracefully(malformed_source_url: str) -> None:
    """_hash_content must handle malformed port in source_url safely without raising."""
    content = """blueprint:
  name: Test Blueprint
  domain: automation
  input: {}
"""
    result = BlueprintUpdateCoordinator._hash_content(content, malformed_source_url)
    assert isinstance(result, str)
    assert len(result) == 64


@pytest.mark.parametrize(
    "invalid_or_non_string_source_url",
    [
        None,
        "",
        "   ",
        12345,
        object(),
        "https://example.com:invalid_port/bp.yaml",
        "https://example.com:99999/bp.yaml",
        "https://[::1/path",
    ],
)
def test_hash_content_and_ensure_source_url_handle_source_url_variants(
    invalid_or_non_string_source_url: object,
) -> None:
    """Test _hash_content and _ensure_source_url handle arbitrary source_url inputs."""
    content = """blueprint:
  name: Test Blueprint
  domain: automation
  input: {}
"""
    hash_result = BlueprintUpdateCoordinator._hash_content(
        content,
        invalid_or_non_string_source_url,
    )
    assert isinstance(hash_result, str)
    assert len(hash_result) == 64

    ensure_result = BlueprintUpdateCoordinator._ensure_source_url(
        content, invalid_or_non_string_source_url
    )
    assert isinstance(ensure_result, str)


@pytest.mark.parametrize(
    "malformed_prev_url",
    [
        "https://example.com:invalid/bp.yaml",
        "https://example.com:70000/bp.yaml",
    ],
)
@pytest.mark.asyncio
async def test_handle_source_url_change_malformed_persisted_url_does_not_raise(
    coordinator: BlueprintUpdateCoordinator,
    mock_makedirs: MagicMock,
    malformed_prev_url: str,
) -> None:
    """Malformed port in persisted source_url must not abort _handle_source_url_change.

    GenericProvider.canonicalize_url preserves the raw netloc when the port is
    malformed, so the persisted URL remains distinct from the port-free curr_url.
    are_same_source therefore returns False → _invalidate_blueprint_metadata clears
    stale remote metadata without raising.
    """
    path = "automation/test_malformed.yaml"
    # Use the host-only form of the URL as the new source — same host, no port.
    curr_url = "https://example.com/bp.yaml"

    coordinator._persisted_metadata = {
        path: {
            "etag": "etag_stale",
            "remote_hash": "hash_stale",
            "source_url": malformed_prev_url,
        }
    }
    prev_info: dict[str, object] = {
        "relative_path": path,
        "source_url": malformed_prev_url,
        "etag": "etag_stale",
        "remote_hash": "hash_stale",
    }
    curr_info: dict[str, object] = {
        "relative_path": path,
        "source_url": curr_url,
        "local_hash": "local_abc",
    }

    # Must not raise even though the persisted URL contains a malformed port.
    result = coordinator._handle_source_url_change(
        path, curr_info, prev_info, prev_url=malformed_prev_url
    )

    # The malformed port is preserved in canonical form → URLs are distinct sources.
    # are_same_source returns False → stale remote metadata is cleared.
    assert result.get("etag") is None
    assert result.get("remote_hash") is None
    assert path not in coordinator._persisted_metadata


def test_scan_single_blueprint_file_skips_non_utf8_file(
    coordinator: BlueprintUpdateCoordinator, tmp_path: Path
) -> None:
    """_scan_single_blueprint_file must skip non-UTF-8 blueprint files without raising."""
    bp_file = tmp_path / "invalid_utf8.yaml"
    bp_file.write_bytes(b"\xff\xfe\xfa\xfb")

    context = BlueprintScanContext(
        hass=coordinator.hass,
        real_blueprint_path=str(tmp_path),
        filter_mode=FilterMode.ALL,
        selected_set=set(),
        max_backups=3,
    )
    with patch(
        "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
        return_value="automation/invalid_utf8.yaml",
    ):
        result = BlueprintUpdateCoordinator._scan_single_blueprint_file(str(bp_file), context)

    assert result is None


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://[::1/invalid_ipv6.yaml",
        "https://example.com:70000/invalid_port.yaml",
    ],
)
def test_scan_single_blueprint_file_skips_malformed_url_value_error(
    coordinator: BlueprintUpdateCoordinator, tmp_path: Path, malformed_url: str
) -> None:
    """_scan_single_blueprint_file must skip files with malformed source_url without raising."""
    content = f"""blueprint:
  name: Malformed URL Blueprint
  domain: automation
  source_url: {malformed_url}
  input: {{}}
"""
    bp_file = tmp_path / "malformed_url.yaml"
    bp_file.write_text(content, encoding="utf-8")

    context = BlueprintScanContext(
        hass=coordinator.hass,
        real_blueprint_path=str(tmp_path),
        filter_mode=FilterMode.ALL,
        selected_set=set(),
        max_backups=3,
    )
    with patch(
        "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
        return_value="automation/malformed_url.yaml",
    ):
        result = BlueprintUpdateCoordinator._scan_single_blueprint_file(str(bp_file), context)

    assert result is None
