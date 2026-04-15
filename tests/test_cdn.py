"""Tests for jsDelivr CDN support in Blueprints Updater."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.blueprints_updater.const import (
    DOMAIN_JSDELIVR,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


@pytest.fixture
def coordinator(hass):
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = {}
    with patch(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.__init__",
        return_value=None,
    ):
        coord = BlueprintUpdateCoordinator(hass, entry, timedelta(hours=24))
        coord.hass = hass
        coord.config_entry = entry
        coord.data = {}
        return coord


@pytest.mark.parametrize(
    "source_url,expected_cdn_url",
    [
        (
            "https://raw.githubusercontent.com/user/repo/main/path/file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@main/path/file.yaml",
        ),
        (
            "https://github.com/user/repo/blob/branch/file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/file.yaml",
        ),
        (
            "https://github.com/user/repo/raw/branch/file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/file.yaml",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/branch/path%20with%20spaces/file%20name.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/path%20with%20spaces/file%20name.yaml",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/branch/dir%28with%29parens/config.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/dir%28with%29parens/config.yaml",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/branch/path/to/file.yaml/",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/path/to/file.yaml",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/branch/path//to//file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@branch/path/to/file.yaml",
        ),
        (
            "https://raw.githubusercontent.com/user/repo/feature/v1/file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@feature/v1/file.yaml",
        ),
        (
            "https://github.com/user/repo/blob/release/v1/subdir/file.yaml",
            f"https://{DOMAIN_JSDELIVR}/gh/user/repo@release/v1/subdir/file.yaml",
        ),
        ("https://github.com/user/repo/raw/", None),
        ("https://raw.githubusercontent.com/u/r/b", None),
        ("https://gist.github.com/user/123/raw", None),
        ("https://example.com/file.yaml", None),
        ("https://raw.githubusercontent.com/short", None),
    ],
)
def test_get_cdn_url(coordinator, source_url, expected_cdn_url):
    """Test GitHub to jsDelivr URL transformation."""
    assert coordinator._get_cdn_url(source_url) == expected_cdn_url


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_success(coordinator):
    """Test successful CDN fetch skips fallback."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"

    coordinator._async_fetch_content = AsyncMock(return_value=("content", "etag"))

    content, etag = await coordinator._async_fetch_with_cdn_fallback(
        session, "path", normalized_url, cdn_url, None, None, False
    )

    assert content == "content"
    assert etag == "etag"
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag=None, force=False
    )


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_304(coordinator):
    """Test successful CDN 304 skips fallback."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"

    coordinator._async_fetch_content = AsyncMock(return_value=(None, "old_etag"))

    content, etag = await coordinator._async_fetch_with_cdn_fallback(
        session, "path", normalized_url, cdn_url, "old_etag", "old_hash", False
    )

    assert content is None
    assert etag == "old_etag"
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag="old_etag", force=False
    )


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_failure_fallback(coordinator):
    """Test CDN failure falls back to original URL."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"

    coordinator._async_fetch_content = AsyncMock(
        side_effect=[httpx.HTTPError("CDN Down"), ("fallback_content", "fallback_etag")]
    )

    content, etag = await coordinator._async_fetch_with_cdn_fallback(
        session, "path", normalized_url, cdn_url, None, None, False
    )

    assert content == "fallback_content"
    assert etag == "fallback_etag"
    assert coordinator._async_fetch_content.call_count == 2

    calls = coordinator._async_fetch_content.call_args_list
    args0, kwargs0 = calls[0]
    assert args0[1] == cdn_url
    assert kwargs0.get("etag") is None
    args1, kwargs1 = calls[1]
    assert args1[1] == normalized_url
    assert kwargs1.get("etag") is None


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_enabled_but_no_cdn_url(coordinator):
    """Test logic skips CDN when URL is not supported even if enabled."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://gist.github.com/user/123/raw"
    cdn_url = None

    coordinator._async_fetch_content = AsyncMock(return_value=("orig", "orig_etag"))

    content, _etag = await coordinator._async_fetch_with_cdn_fallback(
        session, "path", normalized_url, cdn_url, None, None, False
    )

    assert content == "orig"
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, normalized_url, etag=None, force=False
    )


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_etag_only(coordinator):
    """When only stored_etag is set, it should not be reused and CDN fetch should use etag=None."""
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"

    stored_etag = "W/etag-123"
    stored_remote_hash = None

    coordinator._async_fetch_content = AsyncMock(return_value=("content", "new-etag"))

    content, etag = await coordinator._async_fetch_with_cdn_fallback(
        session,
        "path",
        normalized_url,
        cdn_url,
        stored_etag,
        stored_remote_hash,
        False,
    )

    assert content == "content"
    assert etag == "new-etag"
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag=None, force=False
    )


@pytest.mark.asyncio
async def test_async_fetch_with_cdn_fallback_force_ignores_etag_and_hash(coordinator):
    """Test that force=True ignores conditional request data.

    Verifies that with force=True, stored ETag and Hash are ignored
    and etag=None is passed to the underlying fetch method.
    """
    session = MagicMock(spec=httpx.AsyncClient)
    normalized_url = "https://raw.githubusercontent.com/u/r/b/p.yaml"
    cdn_url = "https://cdn.jsdelivr.net/gh/u/r@b/p.yaml"

    stored_etag = "W/etag-123"
    stored_remote_hash = "remote-hash-abc"

    coordinator._async_fetch_content = AsyncMock(return_value=("content", "forced-etag"))

    content, etag = await coordinator._async_fetch_with_cdn_fallback(
        session,
        "path",
        normalized_url,
        cdn_url,
        stored_etag,
        stored_remote_hash,
        True,
    )

    assert content == "content"
    assert etag == "forced-etag"
    coordinator._async_fetch_content.assert_awaited_once_with(
        session, cdn_url, etag=None, force=True
    )
