"""Deterministic snapshots for real-world blueprint integration tests.

The real-world tests exercise compatibility and install parity against popular
upstream blueprints. Fetching those sources on every test run makes the suite
depend on third-party availability and lets an upstream edit change the content
between two network requests. Snapshots are recorded once, committed to the
repository, and replayed by default so the tests stay representative and
deterministic.

Set ``BLUEPRINTS_UPDATER_LIVE_REAL_WORLD=1`` to fetch the sources live. Combine
it with ``BLUEPRINTS_UPDATER_RECORD_REAL_WORLD=1`` to refresh the committed
snapshot file (run with ``-n 0`` to avoid concurrent writers).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import random
import socket
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp
from homeassistant.components.blueprint import importer
from homeassistant.exceptions import ServiceValidationError

from custom_components.blueprints_updater.const import REQUEST_TIMEOUT
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.ha_compat import httpx

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "real_world_blueprints.json.gz"
LIVE_ENV = "BLUEPRINTS_UPDATER_LIVE_REAL_WORLD"
RECORD_ENV = "BLUEPRINTS_UPDATER_RECORD_REAL_WORLD"
SNAPSHOT_VERSION = 1
MAX_FETCH_ATTEMPTS = 4
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524})
_SNAPSHOT_FIELDS = (
    "url",
    "content",
    "canonical_url",
    "author",
    "name",
    "core_suggested_filename",
    "core_raw_data",
)

_SNAPSHOTS: dict[str, BlueprintSnapshot] | None = None


@dataclass(frozen=True)
class BlueprintSnapshot:
    """One real-world blueprint plus the metadata both installers derive."""

    url: str
    content: str
    canonical_url: str
    author: str
    name: str
    core_suggested_filename: str
    core_raw_data: str


def _env_enabled(name: str) -> bool:
    """Return whether an opt-in environment flag is enabled."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def live_mode_enabled() -> bool:
    """Return whether tests should fetch real-world sources live."""
    return _env_enabled(LIVE_ENV)


def _snapshot_key(url: str) -> str:
    """Return the stable snapshot key for a source URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _parse_snapshot(raw: object) -> BlueprintSnapshot:
    """Validate one serialized snapshot record."""
    if not isinstance(raw, dict):
        raise ValueError("Real-world snapshot entry must be an object")
    values: dict[str, str] = {}
    for field in _SNAPSHOT_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str):
            raise ValueError(f"Real-world snapshot field {field!r} must be a string")
        values[field] = value
    return BlueprintSnapshot(
        url=values["url"],
        content=values["content"],
        canonical_url=values["canonical_url"],
        author=values["author"],
        name=values["name"],
        core_suggested_filename=values["core_suggested_filename"],
        core_raw_data=values["core_raw_data"],
    )


def _load_snapshots() -> dict[str, BlueprintSnapshot]:
    """Load and cache all committed snapshots."""
    global _SNAPSHOTS
    if _SNAPSHOTS is not None:
        return _SNAPSHOTS
    if not SNAPSHOT_PATH.is_file():
        raise FileNotFoundError(
            f"Real-world blueprint snapshots are missing: {SNAPSHOT_PATH}. "
            f"Run the suite with {LIVE_ENV}=1 and {RECORD_ENV}=1 to create them."
        )

    try:
        raw = json.loads(gzip.decompress(SNAPSHOT_PATH.read_bytes()))
    except (OSError, EOFError, zlib.error, json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ValueError(
            f"Real-world blueprint snapshot file is unreadable or corrupt: "
            f"{SNAPSHOT_PATH} ({err}). Re-record it with "
            f"{LIVE_ENV}=1 and {RECORD_ENV}=1."
        ) from err
    if not isinstance(raw, dict):
        raise ValueError("Real-world snapshot file must contain an object")
    entries = raw.get("snapshots")
    if not isinstance(entries, dict):
        raise ValueError("Real-world snapshot file must contain a 'snapshots' object")

    snapshots: dict[str, BlueprintSnapshot] = {}
    for key, entry in entries.items():
        if not isinstance(key, str):
            raise ValueError("Real-world snapshot keys must be strings")
        snapshots[key] = _parse_snapshot(entry)
    _SNAPSHOTS = snapshots
    return snapshots


def load_snapshot(url: str) -> BlueprintSnapshot:
    """Return the committed snapshot for a source URL."""
    snapshot = _load_snapshots().get(_snapshot_key(url))
    if snapshot is None or snapshot.url != url:
        raise FileNotFoundError(
            f"No real-world blueprint snapshot for {url!r}. "
            f"Run the suite with {LIVE_ENV}=1 and {RECORD_ENV}=1 to record it."
        )
    return snapshot


def _save_snapshot(snapshot: BlueprintSnapshot) -> None:
    """Merge one snapshot into the committed snapshot file."""
    try:
        snapshots = dict(_load_snapshots())
    except FileNotFoundError:
        snapshots = {}
    existing = snapshots.get(_snapshot_key(snapshot.url))
    if existing is not None and not snapshot.core_suggested_filename:
        if existing.content != snapshot.content:
            # Do not replace a complete snapshot (content plus Core filename
            # metadata) with content recorded without fresh Core metadata.
            return
        snapshot = BlueprintSnapshot(
            url=snapshot.url,
            content=snapshot.content,
            canonical_url=snapshot.canonical_url,
            author=snapshot.author,
            name=snapshot.name,
            core_suggested_filename=existing.core_suggested_filename,
            core_raw_data=existing.core_raw_data,
        )
    if snapshot.content == snapshot.core_raw_data:
        snapshot = BlueprintSnapshot(
            url=snapshot.url,
            content=snapshot.content,
            canonical_url=snapshot.canonical_url,
            author=snapshot.author,
            name=snapshot.name,
            core_suggested_filename=snapshot.core_suggested_filename,
            core_raw_data="",
        )
    snapshots[_snapshot_key(snapshot.url)] = snapshot
    payload = {
        "version": SNAPSHOT_VERSION,
        "snapshots": {key: asdict(value) for key, value in sorted(snapshots.items())},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SNAPSHOT_PATH.with_suffix(".tmp")
    temporary_path.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    temporary_path.replace(SNAPSHOT_PATH)
    global _SNAPSHOTS
    _SNAPSHOTS = snapshots


def _is_transient_error(err: Exception) -> bool:
    """Return whether a fetch error is worth retrying."""
    if isinstance(
        err,
        (
            httpx.TransportError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            TimeoutError,
        ),
    ):
        return True
    if isinstance(err, socket.gaierror):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code in TRANSIENT_STATUS_CODES
    if isinstance(err, aiohttp.ClientResponseError):
        return err.status in TRANSIENT_STATUS_CODES
    if isinstance(err, ServiceValidationError):
        return err.translation_key == "fetch_blueprint_error"
    return False


async def _retry_fetch[T](
    operation: Callable[[], Awaitable[T]],
    description: str,
) -> T:
    """Retry one fetch operation on transient source failures."""
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            return await operation()
        except Exception as err:
            if attempt >= MAX_FETCH_ATTEMPTS or not _is_transient_error(err):
                raise
            delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 1)
            print(f"Retrying {description} after {delay:.1f}s ({err!r})")
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable retry state")


async def _fetch_integration_snapshot(
    coordinator: BlueprintUpdateCoordinator,
    url: str,
) -> BlueprintSnapshot:
    """Fetch one blueprint through the integration and capture its metadata."""
    content, canonical_url, author, name, _response = await _retry_fetch(
        lambda: coordinator.async_fetch_import_data(url),
        f"integration import for {url}",
    )
    return BlueprintSnapshot(
        url=url,
        content=content,
        canonical_url=canonical_url,
        author=author,
        name=name,
        core_suggested_filename="",
        core_raw_data="",
    )


async def _fetch_core_blueprint(
    coordinator: BlueprintUpdateCoordinator,
    url: str,
) -> importer.ImportedBlueprint:
    """Fetch one blueprint through the Home Assistant Core importer."""
    async with asyncio.timeout(REQUEST_TIMEOUT):
        return await importer.fetch_blueprint_from_url(coordinator.hass, url)


async def fetch_live_snapshot(
    coordinator: BlueprintUpdateCoordinator,
    url: str,
    *,
    integration_snapshot: BlueprintSnapshot | None = None,
) -> BlueprintSnapshot:
    """Fetch one blueprint from its source and capture both installers' metadata."""
    imported = await _retry_fetch(
        lambda: _fetch_core_blueprint(coordinator, url),
        f"HA Core import for {url}",
    )
    snapshot = integration_snapshot or await _fetch_integration_snapshot(coordinator, url)
    if snapshot.content != imported.raw_data:
        raise ValueError(
            f"Upstream content changed between the HA Core and integration fetches for "
            f"{url!r}; retry recording once the source is stable."
        )
    return BlueprintSnapshot(
        url=snapshot.url,
        content=snapshot.content,
        canonical_url=snapshot.canonical_url,
        author=snapshot.author,
        name=snapshot.name,
        core_suggested_filename=imported.suggested_filename,
        core_raw_data=imported.raw_data,
    )


async def get_snapshot(
    coordinator: BlueprintUpdateCoordinator,
    url: str,
) -> BlueprintSnapshot:
    """Return a snapshot, fetching live only when explicitly requested."""
    if not live_mode_enabled():
        return load_snapshot(url)

    snapshot = await fetch_live_snapshot(coordinator, url)
    if _env_enabled(RECORD_ENV):
        _save_snapshot(snapshot)
    return snapshot


async def get_blueprint_content(
    coordinator: BlueprintUpdateCoordinator,
    url: str,
) -> str:
    """Return blueprint content for compatibility validation."""
    if not live_mode_enabled():
        return load_snapshot(url).content

    if _env_enabled(RECORD_ENV):
        try:
            existing = load_snapshot(url)
        except FileNotFoundError:
            existing = None
        snapshot = await _fetch_integration_snapshot(coordinator, url)
        expected_core_filename = f"{snapshot.author}/{snapshot.name}"
        if (
            existing is None
            or existing.content != snapshot.content
            or existing.core_suggested_filename != expected_core_filename
        ):
            # Content is new or changed, or the recorded Core filename no longer
            # matches the integration metadata: fetch fresh Core metadata before
            # it can be committed. Reuse the integration fetch to avoid a second
            # gist/API request.
            snapshot = await fetch_live_snapshot(
                coordinator,
                url,
                integration_snapshot=snapshot,
            )
        _save_snapshot(snapshot)
        return snapshot.content

    content, _, _, _, _ = await _retry_fetch(
        lambda: coordinator.async_fetch_import_data(url),
        f"compatibility fetch for {url}",
    )
    return content
