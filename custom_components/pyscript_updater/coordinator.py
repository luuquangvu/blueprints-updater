"""Data coordinator for Pyscript Updater."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_AUTO_UPDATE,
    CONF_GITHUB_TOKEN,
    CONF_MANIFEST_FILE,
    CONF_PYSCRIPT_DIR,
    CONF_RELOAD_AFTER_UPDATE,
    DEFAULT_MANIFEST_FILE,
    DEFAULT_PYSCRIPT_DIR,
    DEFAULT_RELOAD_AFTER_UPDATE,
    DOMAIN,
    MAX_CONCURRENT_REQUESTS,
    RE_FILE_EXT,
    RE_GITHUB_BLOB,
    RE_GITHUB_RAW,
    RE_GITHUB_TREE,
    REQUEST_TIMEOUT,
    STORAGE_KEY_DATA,
    STORAGE_VERSION,
)
from .utils import get_max_backups, get_option

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestEntry:
    """A parsed line from the manifest file."""

    url: str
    dest: str
    recursive: bool


def _sha256(data: bytes) -> str:
    """Return SHA256 hex digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


class PyscriptUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Manage fetching pyscript file updates from GitHub."""

    config_entry: ConfigEntry
    data: dict[str, dict[str, Any]]

    @staticmethod
    def generate_unique_id(entry_id: str, rel_path: str) -> str:
        """Return a deterministic unique id for an entry + relative path."""
        combined = f"{entry_id}_{rel_path}"
        return f"pyscript_{hashlib.sha256(combined.encode()).hexdigest()}"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
            config_entry=entry,
        )
        self.config_entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DATA)
        self._persisted: dict[str, Any] = {}
        self._translations: dict[str, str] = {}
        self._translations_lang: str | None = None
        self.setup_complete = False
        self.data = {}

    # ------------------------------------------------------------------
    # Setup / persistence
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Restore persisted data from storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._persisted = stored

    async def async_shutdown(self) -> None:
        """Persist state on shutdown."""
        await self._persist()

    async def _persist(self) -> None:
        """Write cache state to disk."""
        cache = {
            rel_path: {
                "remote_hash": info.get("remote_hash"),
                "etag": info.get("etag"),
            }
            for rel_path, info in self.data.items()
        }
        await self._store.async_save(cache)

    # ------------------------------------------------------------------
    # Translations
    # ------------------------------------------------------------------
    def clear_translations(self) -> None:
        """Invalidate translation cache."""
        self._translations = {}
        self._translations_lang = None

    async def async_translate(self, key: str, category: str = "common", **kwargs: Any) -> str:
        """Translate a key using HA translations with simple formatting."""
        lang = getattr(self.hass.config, "language", "en")
        if lang != self._translations_lang:
            try:
                translations = await async_get_translations(self.hass, lang, category, [DOMAIN])
            except (OSError, ValueError) as err:
                _LOGGER.debug("Failed to load translations: %s", err)
                translations = {}
            self._translations = translations
            self._translations_lang = lang

        msg = (
            self._translations.get(f"component.{DOMAIN}.{category}.{key}.message")
            or self._translations.get(f"component.{DOMAIN}.{category}.{key}")
            or key
        )
        try:
            return msg.format(**kwargs) if kwargs else msg
        except (KeyError, ValueError, IndexError):
            return msg

    # ------------------------------------------------------------------
    # Manifest parsing
    # ------------------------------------------------------------------
    def _resolve_paths(self) -> tuple[str, str]:
        """Return (pyscript_dir, manifest_path)."""
        pyscript_dir = get_option(self.config_entry, CONF_PYSCRIPT_DIR, DEFAULT_PYSCRIPT_DIR)
        manifest_name = get_option(self.config_entry, CONF_MANIFEST_FILE, DEFAULT_MANIFEST_FILE)

        if os.path.isabs(manifest_name):
            manifest_path = manifest_name
        else:
            manifest_path = os.path.join(pyscript_dir, manifest_name)
        return pyscript_dir, manifest_path

    def _read_manifest(self, manifest_path: str) -> list[ManifestEntry]:
        """Parse the manifest file into a list of entries."""
        if not os.path.exists(manifest_path):
            return []

        entries: list[ManifestEntry] = []
        with open(manifest_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    _LOGGER.warning("Invalid manifest line: %s", line)
                    continue
                url = parts[0]
                dest = parts[1]
                option = parts[2].lower() if len(parts) >= 3 else ""
                entries.append(
                    ManifestEntry(
                        url=url,
                        dest=dest,
                        recursive=(option == "recursive"),
                    )
                )
        return entries

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_raw_url(url: str) -> str:
        """Convert a github blob URL into a raw URL."""
        m = RE_GITHUB_BLOB.match(url)
        if m:
            user, repo, ref, path = m.groups()
            return f"https://raw.githubusercontent.com/{user}/{repo}/{ref}/{path}"
        return url

    @staticmethod
    def _to_api_url(tree_url: str) -> str | None:
        """Convert a github tree URL into a contents API URL."""
        m = RE_GITHUB_TREE.match(tree_url)
        if not m:
            return None
        user, repo, ref, path = m.groups()
        path = (path or "").strip("/")
        if path:
            return f"https://api.github.com/repos/{user}/{repo}/contents/{path}?ref={ref}"
        return f"https://api.github.com/repos/{user}/{repo}/contents?ref={ref}"

    @staticmethod
    def _detect_url_kind(url: str, dest: str) -> str:
        """Return 'file' or 'folder'."""
        if dest.endswith("/"):
            return "folder"
        if RE_GITHUB_TREE.match(url):
            return "folder"
        if RE_GITHUB_BLOB.match(url) or RE_GITHUB_RAW.match(url):
            return "file"
        if RE_FILE_EXT.search(url):
            return "file"
        return "file"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return HTTP headers (with optional GitHub token)."""
        headers = {"Accept": "application/vnd.github+json"}
        token = get_option(self.config_entry, CONF_GITHUB_TOKEN, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    async def _fetch_json(self, url: str) -> Any:
        """GET JSON from an API endpoint."""
        client: httpx.AsyncClient = get_async_client(self.hass)
        resp = await client.get(url, headers=self._auth_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    async def async_fetch_bytes(
        self, url: str, etag: str | None = None
    ) -> tuple[bytes | None, str | None, int]:
        """GET raw bytes. Returns (content|None on 304, etag, status)."""
        client: httpx.AsyncClient = get_async_client(self.hass)
        extra: dict[str, str] = {}
        if etag:
            extra["If-None-Match"] = etag
        resp = await client.get(url, headers=self._auth_headers(extra), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 304:
            return None, etag, 304
        resp.raise_for_status()
        return resp.content, resp.headers.get("ETag"), resp.status_code

    # ------------------------------------------------------------------
    # Target expansion (manifest -> list of concrete files)
    # ------------------------------------------------------------------
    async def _expand_manifest(self, entries: list[ManifestEntry]) -> list[dict[str, Any]]:
        """Expand manifest (files + folders) into a flat list of file targets."""
        targets: list[dict[str, Any]] = []

        for entry in entries:
            kind = self._detect_url_kind(entry.url, entry.dest)
            if kind == "file":
                targets.append(
                    {
                        "raw_url": self._to_raw_url(entry.url),
                        "source_url": entry.url,
                        "rel_path": entry.dest.strip("/"),
                    }
                )
                continue

            try:
                folder_targets = await self._expand_folder(
                    entry.url, entry.dest.rstrip("/"), entry.recursive
                )
                targets.extend(folder_targets)
            except (httpx.HTTPError, ValueError) as err:
                _LOGGER.error("Failed to expand folder %s: %s", entry.url, err)

        return targets

    async def _expand_folder(
        self, folder_url: str, dest_prefix: str, recursive: bool
    ) -> list[dict[str, Any]]:
        """Expand a github folder URL into a list of file targets."""
        api_url = self._to_api_url(folder_url)
        if not api_url:
            raise ValueError(f"Invalid folder URL: {folder_url}")

        data = await self._fetch_json(api_url)
        if isinstance(data, dict) and "message" in data:
            raise ValueError(f"GitHub API error: {data['message']}")
        if not isinstance(data, list):
            raise ValueError(f"Unexpected API response for {api_url}")

        m = RE_GITHUB_TREE.match(folder_url)
        if not m:
            return []
        user, repo, ref, _ = m.groups()

        targets: list[dict[str, Any]] = []
        for item in data:
            item_type = item.get("type")
            name = item.get("name")
            if not name:
                continue
            rel = f"{dest_prefix}/{name}" if dest_prefix else name

            if item_type == "file":
                download_url = item.get("download_url") or (
                    f"https://raw.githubusercontent.com/{user}/{repo}/{ref}/{item.get('path')}"
                )
                source_url = f"https://github.com/{user}/{repo}/blob/{ref}/{item.get('path')}"
                targets.append(
                    {
                        "raw_url": download_url,
                        "source_url": source_url,
                        "rel_path": rel,
                    }
                )
            elif item_type == "dir" and recursive:
                sub_url = f"https://github.com/{user}/{repo}/tree/{ref}/{item.get('path')}"
                targets.extend(await self._expand_folder(sub_url, rel, True))

        return targets

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Refresh state: parse manifest, fetch remote hashes, compare local."""
        pyscript_dir, manifest_path = self._resolve_paths()

        entries = await self.hass.async_add_executor_job(self._read_manifest, manifest_path)

        targets = await self._expand_manifest(entries)

        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        prev_data = self.data or {}

        async def _process(target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            rel_path = target["rel_path"]
            raw_url = target["raw_url"]
            source_url = target["source_url"]

            prev = prev_data.get(rel_path) or self._persisted.get(rel_path) or {}
            prev_etag = prev.get("etag")
            prev_remote_hash = prev.get("remote_hash")

            local_path = os.path.join(pyscript_dir, rel_path)
            local_exists = os.path.exists(local_path)
            local_hash: str | None = None
            if local_exists:
                try:
                    local_bytes = await self.hass.async_add_executor_job(_read_bytes, local_path)
                    local_hash = _sha256(local_bytes)
                except OSError as err:
                    _LOGGER.warning("Failed to read %s: %s", local_path, err)

            last_error: str | None = None
            remote_hash: str | None = None
            remote_content: bytes | None = None
            etag: str | None = prev_etag

            async with sem:
                try:
                    content, new_etag, status = await self.async_fetch_bytes(
                        raw_url, etag=prev_etag
                    )
                    if status == 304 and prev_remote_hash:
                        remote_hash = prev_remote_hash
                        etag = prev_etag
                    elif content is not None:
                        remote_content = content
                        remote_hash = _sha256(content)
                        etag = new_etag
                except httpx.HTTPStatusError as err:
                    last_error = f"fetch_error|HTTP {err.response.status_code}"
                except httpx.HTTPError as err:
                    last_error = f"fetch_error|{err}"
                except (OSError, ValueError) as err:
                    last_error = f"processing_error|{err}"

            updatable = remote_hash is not None and (not local_exists or local_hash != remote_hash)

            return rel_path, {
                "rel_path": rel_path,
                "source_url": source_url,
                "raw_url": raw_url,
                "local_hash": local_hash or "",
                "remote_hash": remote_hash,
                "remote_content": remote_content,
                "etag": etag,
                "local_exists": local_exists,
                "updatable": updatable,
                "last_error": last_error,
            }

        results = await asyncio.gather(*(_process(t) for t in targets))
        data = dict(results)

        self._persisted = {
            rel: {"remote_hash": info.get("remote_hash"), "etag": info.get("etag")}
            for rel, info in data.items()
        }
        with contextlib.suppress(Exception):
            await self._store.async_save(self._persisted)

        if self.setup_complete and get_option(self.config_entry, CONF_AUTO_UPDATE, False):
            await self._run_auto_update(pyscript_dir, data)

        return data

    async def _run_auto_update(self, pyscript_dir: str, data: dict[str, dict[str, Any]]) -> None:
        """Install every available update, back up first."""
        updated: list[str] = []
        for rel_path, info in list(data.items()):
            if not info.get("updatable"):
                continue
            content = info.get("remote_content")
            if content is None:
                try:
                    content_bytes, etag, _ = await self.async_fetch_bytes(info["raw_url"])
                    if content_bytes is None:
                        continue
                    content = content_bytes
                    info["remote_content"] = content
                    info["etag"] = etag
                except httpx.HTTPError as err:
                    _LOGGER.warning("Auto-update fetch failed for %s: %s", rel_path, err)
                    continue
            try:
                await self.async_install_file(rel_path, content, reload_after=False, backup=True)
                updated.append(rel_path)
            except OSError as err:
                _LOGGER.error("Auto-update install failed for %s: %s", rel_path, err)
                info["last_error"] = f"install_error|{err}"

        if updated and get_option(
            self.config_entry, CONF_RELOAD_AFTER_UPDATE, DEFAULT_RELOAD_AFTER_UPDATE
        ):
            await self.async_reload_pyscript()

    # ------------------------------------------------------------------
    # Installation / restore
    # ------------------------------------------------------------------
    def _backup_rotate(self, dest: str, max_backups: int) -> None:
        """Rotate backups: .bak.N -> .bak.N+1 (drop highest), current -> .bak.1."""
        if not os.path.exists(dest):
            return

        oldest = f"{dest}.bak.{max_backups}"
        if os.path.exists(oldest):
            try:
                os.remove(oldest)
            except OSError as err:
                _LOGGER.warning("Failed to remove oldest backup %s: %s", oldest, err)

        for i in range(max_backups - 1, 0, -1):
            src = f"{dest}.bak.{i}"
            dst = f"{dest}.bak.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError as err:
                    _LOGGER.warning("Backup rotate failed %s -> %s: %s", src, dst, err)

        try:
            shutil.copy2(dest, f"{dest}.bak.1")
        except OSError as err:
            _LOGGER.warning("Failed to create backup for %s: %s", dest, err)

    async def async_install_file(
        self,
        rel_path: str,
        content: bytes,
        reload_after: bool = True,
        backup: bool = True,
    ) -> None:
        """Write remote content to disk, optionally back up and reload pyscript."""
        pyscript_dir, _ = self._resolve_paths()
        dest = os.path.join(pyscript_dir, rel_path)

        def _write() -> None:
            os.makedirs(os.path.dirname(dest) or pyscript_dir, exist_ok=True)
            if backup:
                self._backup_rotate(dest, get_max_backups(self.config_entry))
            tmp = f"{dest}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(content)
            os.replace(tmp, dest)

        await self.hass.async_add_executor_job(_write)

        info = self.data.get(rel_path)
        if info:
            info["local_hash"] = _sha256(content)
            info["local_exists"] = True
            info["updatable"] = False
            info["remote_content"] = None
            info["last_error"] = None

        if reload_after and get_option(
            self.config_entry, CONF_RELOAD_AFTER_UPDATE, DEFAULT_RELOAD_AFTER_UPDATE
        ):
            await self.async_reload_pyscript()

    async def async_restore_file(self, rel_path: str, version: int = 1) -> dict[str, Any]:
        """Restore rel_path from a numbered backup (.bak.N)."""
        pyscript_dir, _ = self._resolve_paths()
        dest = os.path.join(pyscript_dir, rel_path)
        backup_path = f"{dest}.bak.{version}"

        def _check_and_restore() -> str | None:
            if not os.path.exists(backup_path):
                return "missing_backup"
            try:
                shutil.copy2(backup_path, dest)
                return None
            except OSError as err:
                return f"system_error|{err}"

        error = await self.hass.async_add_executor_job(_check_and_restore)
        if error:
            return {"translation_key": error.split("|")[0], "success": False}

        await self.async_reload_pyscript()
        await self.async_request_refresh()
        return {"translation_key": "restore_success", "success": True}

    async def async_reload_pyscript(self) -> None:
        """Call pyscript.reload if available."""
        if self.hass.services.has_service("pyscript", "reload"):
            try:
                await self.hass.services.async_call("pyscript", "reload", {}, blocking=True)
            except (OSError, RuntimeError) as err:
                _LOGGER.warning("pyscript.reload failed: %s", err)
        else:
            _LOGGER.debug("pyscript.reload service not registered")


def _read_bytes(path: str) -> bytes:
    """Blocking helper to read a whole file into memory."""
    with open(path, "rb") as fh:
        return fh.read()
