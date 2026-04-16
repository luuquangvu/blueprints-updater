"""Data coordinator for Blueprints Updater."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import html
import ipaddress
import logging
import os
import random
import shutil
import socket
import textwrap
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypedDict
from urllib.parse import urlparse, urlunparse

import httpx
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import yaml as yaml_util
from homeassistant.util.ssl import SSL_ALPN_HTTP11_HTTP2

from .const import (
    ALLOWED_RELOAD_DOMAINS,
    CONF_AUTO_UPDATE,
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    CONF_USE_CDN,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_USE_CDN,
    DOMAIN,
    DOMAIN_GIST,
    DOMAIN_GITHUB,
    DOMAIN_GITHUB_RAW,
    DOMAIN_HA_FORUM,
    DOMAIN_JSDELIVR,
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
    RE_BLUEPRINT_KEY,
    RE_FORUM_CODE_BLOCK,
    RE_FORUM_TOPIC_ID,
    RE_GIST_RAW,
    RE_GITHUB_BLOB,
    RE_URL_REDACTION,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    SPECIAL_USE_TLDS,
    STORAGE_KEY_DATA,
    STORAGE_VERSION,
)
from .utils import get_max_backups, retry_async

_LOGGER = logging.getLogger(__name__)


def _sanitize_error_detail(detail: str, max_length: int = 120) -> str:
    """
    Sanitize an error-detail string by redacting URLs, replacing delimiter characters, and truncating to a maximum length.
    
    Redacts URLs, replaces the pipe character `|` with `/`, and shortens the result to `max_length` characters using an ellipsis when necessary.
    
    Parameters:
        detail (str): The raw error message string to sanitize.
        max_length (int): Maximum allowed length for the sanitized string.
    
    Returns:
        str: The sanitized and possibly truncated error string.
    """
    cleaned = RE_URL_REDACTION.sub("(redacted URL)", detail)
    cleaned = cleaned.replace("|", "/")
    return textwrap.shorten(cleaned, width=max_length, placeholder="...")


class ParsedBlueprintData(TypedDict):
    """Data extracted from a blueprint YAML file."""

    name: str
    domain: str
    source_url: str
    local_hash: str


class BlueprintMetadata(ParsedBlueprintData):
    """Augmented blueprint data from file scanning."""

    rel_path: str


@dataclass(frozen=True)
class GitDiffResult:
    """Structure for git diff generation results."""

    diff_text: str
    is_semantic_sync: bool


class BlueprintUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Class to manage fetching blueprint updates."""

    @staticmethod
    def generate_unique_id(entry_id: str, rel_path: str) -> str:
        """Generate a deterministic unique ID from an entry ID and a blueprint's relative path.

        Args:
            entry_id: The config entry ID.
            rel_path: The blueprint's relative path.

        Returns:
            The generated unique ID.

        """
        combined = f"{entry_id}_{rel_path}"
        return f"blueprint_{hashlib.sha256(combined.encode()).hexdigest()}"

    @staticmethod
    def generate_legacy_unique_id(rel_path: str) -> str:
        """Generate a legacy unique ID from rel_path only.

        Args:
            rel_path: The blueprint's relative path.

        Returns:
            The legacy generated unique ID.

        """
        return f"blueprint_{hashlib.sha256(rel_path.encode()).hexdigest()}"

    config_entry: ConfigEntry
    data: dict[str, dict[str, Any]]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance.
            entry: Integration configuration entry.
            update_interval: Scan interval.

        """
        self.hass = hass
        self.config_entry = entry
        self.data: dict[str, dict[str, Any]] = {}
        self.setup_complete = False
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self._translations: dict[tuple[str, str], dict[str, str]] = {}
        self.hass.data.setdefault(DOMAIN, {}).setdefault("translation_cache", {})
        self._translation_lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._pacing_lock = asyncio.Lock()
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DATA)
        self._persisted_etags: dict[str, str] = {}
        self._persisted_hashes: dict[str, str] = {}
        self._safe_hostname_cache: dict[str, bool] = {}
        self._safe_hostname_lock = asyncio.Lock()
        self._first_update_done = False
        if self.config_entry:
            self.config_entry.async_on_unload(self._async_cancel_background_task)

    def clear_translations(self) -> None:
        """Clear the internal translation cache.

        This method resets the coordinator's translation dictionary, allowing
        it to be re-populated on the next translation request.
        """
        _LOGGER.debug("Clearing translations for Blueprints Updater coordinator")
        self._translations = {}

    async def async_setup(self) -> None:
        """Load persisted data from storage.

        This method reads the stored ETags and remote hashes from the
        local filesystem to restore the state between restarts.
        """
        storage_data = await self._store.async_load()
        if storage_data and isinstance(storage_data, dict):
            persisted_etags = storage_data.get("etags") or {}
            persisted_hashes = storage_data.get("remote_hashes") or {}

            if not isinstance(persisted_etags, dict):
                _LOGGER.warning(
                    "Ignoring invalid persisted etags in storage; expected dict, got %s",
                    type(persisted_etags).__name__,
                )
                persisted_etags = {}
            else:
                valid_etags = {
                    k: v
                    for k, v in persisted_etags.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
                if len(valid_etags) != len(persisted_etags):
                    _LOGGER.warning(
                        "Dropped %d invalid ETag entries from storage (non-string keys or values)",
                        len(persisted_etags) - len(valid_etags),
                    )
                persisted_etags = valid_etags

            if not isinstance(persisted_hashes, dict):
                _LOGGER.warning(
                    "Ignoring invalid persisted remote_hashes in storage; expected dict, got %s",
                    type(persisted_hashes).__name__,
                )
                persisted_hashes = {}
            else:
                valid_hashes = {
                    k: v
                    for k, v in persisted_hashes.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
                if len(valid_hashes) != len(persisted_hashes):
                    _LOGGER.warning(
                        "Dropped %d invalid remote hash entries from storage",
                        len(persisted_hashes) - len(valid_hashes),
                    )
                persisted_hashes = valid_hashes

            self._persisted_etags = persisted_etags
            self._persisted_hashes = persisted_hashes

            _LOGGER.debug(
                "Loaded %d persisted ETags and %d remote hashes",
                len(self._persisted_etags),
                len(self._persisted_hashes),
            )

        self.setup_complete = True

    async def async_translate(self, key: str, category: str = "common", **kwargs: Any) -> str:
        """Translate a key using the current language and category.

        This method is a wrapper around async_get_translations that provides
        a more convenient API and better error handling for startup race conditions.

        Args:
            key: Translation key.
            category: Translation category (common, exceptions, etc.).
            **kwargs: Template arguments for the translation string.

        Returns:
            Translated and formatted string.

        """
        language = getattr(self.hass.config, "language", "en")
        cache_key = (language, category)

        if not self.setup_complete:
            return key

        if cache_key not in self._translations:
            async with self._translation_lock:
                if cache_key not in self._translations:
                    try:
                        loaded = await async_get_translations(
                            self.hass, language, category, [DOMAIN]
                        )
                        self._translations[cache_key] = loaded or {}
                        if loaded:
                            _LOGGER.debug(
                                "Successfully loaded translations for language: %s, category: %s",
                                language,
                                category,
                            )
                    except (OSError, ValueError) as err:
                        _LOGGER.debug(
                            "Could not load translations for %s (%s) for language %s: %s",
                            DOMAIN,
                            category,
                            language,
                            err,
                        )
                        self._translations[cache_key] = {}

        translations = self._translations.get(cache_key, {})

        search_categories = [category]
        extra_cats = [
            "common",
            "exceptions",
            "selector",
            "title",
            "config",
            "options",
            "services",
            "entity",
            "device",
            "device_automation",
            "entity_component",
            "issues",
        ]
        for cat in extra_cats:
            if cat not in search_categories:
                search_categories.append(cat)

        template = None
        for cat in search_categories:
            full_key = f"component.{DOMAIN}.{cat}.{key}"
            template = translations.get(f"{full_key}.message") or translations.get(full_key)
            if template:
                break

        if not template:
            template = key
            _LOGGER.debug("Translation key not found: %s in search path %s", key, search_categories)

        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, ValueError, IndexError) as err:
            _LOGGER.debug(
                "Error formatting translation for key %s in categories %s: %s",
                key,
                search_categories,
                err,
            )
            return template

    def _get_scan_config(self) -> tuple[str, list[str]]:
        """Extract and validate filtering configuration from the entry.

        Returns:
            A tuple of (filter_mode, selected_blueprints).

        """
        filter_mode = self._get_validated_filter_mode(
            self.config_entry.options.get(CONF_FILTER_MODE, FILTER_MODE_ALL)
            if self.config_entry
            else FILTER_MODE_ALL
        )
        selected_blueprints = self._get_validated_selected_blueprints(
            self.config_entry.options.get(CONF_SELECTED_BLUEPRINTS, []) if self.config_entry else []
        )
        return filter_mode, selected_blueprints

    @staticmethod
    def _filter_existing_metadata(
        paths: set[str], etags_map: dict[str, str], hashes_map: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Filter metadata maps to only include paths that exist on disk.

        This is a synchronous method intended to be run in an executor.

        Args:
            paths: Set of paths to verify.
            etags_map: Map of path to ETag.
            hashes_map: Map of path to remote hash.

        Returns:
            A tuple of (filtered_etags, filtered_hashes).

        """
        valid_set = {p for p in paths if os.path.isfile(p)}
        return (
            {p: e for p, e in etags_map.items() if p in valid_set},
            {p: h for p, h in hashes_map.items() if p in valid_set},
        )

    async def _async_prune_stale_metadata(self, scanned_paths: set[str]) -> None:
        """Remove metadata for blueprints that no longer exist on disk.

        This method synchronizes in-memory ETag and Hash caches with the
        latest scan results. We preserve metadata for any path that
        either returned in the current scan or still exists as a file on
        the disk. This ensures that metadata for valid blueprints is not
        purged if they are temporarily filtered out of the scan results
        due to user configuration changes (e.g., filter mode or selection),
        providing a more stable cache and better UX.

        To prevent blocking the event loop, file existence checks are
        performed in the executor.

        Args:
            scanned_paths: Set of absolute paths found on disk during
                the latest scan.

        """
        old_count = len(self._persisted_etags) + len(self._persisted_hashes)

        all_metadata_paths = set(self._persisted_etags.keys()) | set(self._persisted_hashes.keys())

        if paths_to_verify := all_metadata_paths - scanned_paths:
            existing_etags, existing_hashes = await self.hass.async_add_executor_job(
                self._filter_existing_metadata,
                paths_to_verify,
                self._persisted_etags,
                self._persisted_hashes,
            )
            existing_paths = set(existing_etags.keys()) | set(existing_hashes.keys())
        else:
            existing_paths = set()

        valid_paths = scanned_paths | existing_paths

        self._persisted_etags = {
            path: etag for path, etag in self._persisted_etags.items() if path in valid_paths
        }
        self._persisted_hashes = {
            path: r_hash for path, r_hash in self._persisted_hashes.items() if path in valid_paths
        }

        if (len(self._persisted_etags) + len(self._persisted_hashes)) < old_count:
            _LOGGER.debug("Pruned stale blueprint metadata from memory, triggering save")
            self.data = {path: info for path, info in self.data.items() if path in valid_paths}
            self.hass.async_create_background_task(
                self._async_save_metadata(force=True), name=f"{DOMAIN}_prune_save"
            )

    async def _async_initialize_results(
        self, blueprints: dict[str, BlueprintMetadata]
    ) -> dict[str, dict[str, Any]]:
        """Create the initial results structure from disk scan.

        Pre-populates basic metadata and local hashes. Remote metadata
        is only restored from disk persistence if this is the first scan
        after startup (triggered by _first_update_done).

        Args:
            blueprints: Metadata mapping from scan_blueprints.

        Returns:
            A results dictionary indexed by path.

        """
        await self._async_prune_stale_metadata(set(blueprints.keys()))
        return {
            path: {
                "name": info["name"],
                "rel_path": info["rel_path"],
                "domain": info["domain"],
                "source_url": info["source_url"],
                "local_hash": info["local_hash"],
                "updatable": False,
                "remote_hash": None
                if self._first_update_done
                else self._persisted_hashes.get(path),
                "invalid_remote_hash": None,
                "remote_content": None,
                "last_error": None,
                "etag": None if self._first_update_done else self._persisted_etags.get(path),
            }
            for path, info in blueprints.items()
        }

    def _merge_previous_data(self, results: dict[str, dict[str, Any]]) -> None:
        """Merge previous scan metadata and detect synchronization issues.

        This method synchronizes current scan results with the existing
        coordinator data to maintain continuity for ETags and remote content.
        It also implements "ghost update" detection which suppresses update
        notifications when contents are effectively identical after
        canonical normalization.

        Args:
            results: The newly initialized results dictionary to update.

        """
        if not self.data:
            for info in results.values():
                if info.get("remote_hash"):
                    is_mismatch = info["local_hash"] != info["remote_hash"]
                    info["updatable"] = is_mismatch
                    if is_mismatch:
                        info["etag"] = None
            return

        for path, info in results.items():
            if path in self.data:
                prev = self._handle_source_url_change(path, info, self.data[path])

                is_updatable, next_invalid, next_error, next_remote = (
                    self._apply_ghost_update_detection(path, info, prev)
                )

                info.update(
                    {
                        "updatable": is_updatable,
                        "remote_hash": next_remote,
                        "invalid_remote_hash": next_invalid,
                        "remote_content": prev.get("remote_content"),
                        "last_error": next_error,
                        "etag": prev.get("etag"),
                    }
                )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch and synchronize blueprint update data.

        Performs a fast local disk scan to identify blueprints and
        synchronize them with persisted remote metadata. Results are
        returned immediately for UI responsiveness, while an exhaustive
        remote update is triggered in the background.

        Returns:
            A dictionary containing blueprint information and update status.

        """
        filter_mode, selected = self._get_scan_config()

        _LOGGER.debug(
            "Starting fast local blueprint scan (filter_mode=%s)",
            filter_mode,
        )

        blueprints = await self.hass.async_add_executor_job(
            self.scan_blueprints,
            self.hass,
            filter_mode,
            selected,
        )

        results = await self._async_initialize_results(blueprints)
        self._merge_previous_data(results)

        self.data = results
        self._first_update_done = True
        self._start_background_refresh(blueprints)

        _LOGGER.debug("Instant setup complete with %d blueprints", len(results))
        return results

    def _is_semantically_equal(
        self, content: Any, target_hash: Any, already_normalized: bool = False
    ) -> bool:
        """Check if content is semantically equal to a target hash.

        Normalization is applied to content before hashing if needed.

        Args:
            content: Raw content string to verify.
            target_hash: The hash to compare against.
            already_normalized: If True, skip normalization (optimization).

        Returns:
            True if normalized content hash matches target_hash.

        """
        if not content or not isinstance(content, str):
            return False
        return self._hash_content(content, already_normalized=already_normalized) == target_hash

    def _handle_source_url_change(
        self, path: str, info: dict[str, Any], prev: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle detected change in blueprint source URL.

        If the URL changed, invalidate all remote-derived metadata to
        prevent stale state reuse.

        Args:
            path: Local path of the blueprint.
            info: Newly scanned blueprint info.
            prev: Previous metadata dictionary.

        Returns:
            Updated (possibly invalidated) metadata dictionary.

        """
        prev_url = prev.get("source_url")
        curr_url = info.get("source_url")

        if prev_url and curr_url and prev_url != curr_url:
            _LOGGER.info(
                "Source URL changed for %s (%s -> %s); clearing remote cache",
                path,
                prev_url,
                curr_url,
            )
            return {
                **prev,
                "remote_hash": None,
                "invalid_remote_hash": None,
                "remote_content": None,
                "last_error": None,
                "etag": None,
            }
        return prev

    def _apply_ghost_update_detection(
        self, path: str, info: dict[str, Any], prev_data: dict[str, Any]
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Apply ghost update detection to a blueprint.

        If a ghost update is detected, updatable is set to False and the
        remote_hash is synced to the local_hash.

        Args:
            path: Local path of the blueprint.
            info: Newly scanned blueprint info.
            prev_data: Previous metadata dictionary.

        Returns:
            A tuple of (is_updatable, next_invalid_remote_hash, next_last_error, next_remote_hash).

        """
        local_hash = info["local_hash"]
        remote_hash = info.get("remote_hash") or prev_data.get("remote_hash")
        is_updatable = bool(remote_hash and local_hash != remote_hash)
        next_invalid = prev_data.get("invalid_remote_hash")
        next_error = prev_data.get("last_error")

        if is_updatable and self._is_ghost_update(local_hash, prev_data):
            _LOGGER.debug("Ghost update detected for %s; forcing updatable=False", path)
            return False, None, None, local_hash

        return is_updatable, next_invalid, next_error, remote_hash

    def _is_ghost_update(self, current_local_hash: Any, prev_data: dict[str, Any]) -> bool:
        """Check if a detected update is actually a 'ghost update'.

        A ghost update occurs when the content is effectively identical
        to the local version after transport-level normalization, but
        the previous hashes were out of sync.

        Args:
            current_local_hash: The hash of the freshly scanned local file.
            prev_data: Previous metadata dictionary for this path.

        Returns:
            True if the cached remote content matches the local hash.

        """
        return self._is_semantically_equal(prev_data.get("remote_content"), current_local_hash)

    def _start_background_refresh(self, blueprints: dict[str, Any]) -> None:
        """Start the background remote refresh task if not already running.

        Args:
            blueprints: Dictionary of blueprints to scan remotely.

        """
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Background refresh already in progress, skipping start")
            return

        self._background_task = self.hass.async_create_background_task(
            self._async_background_refresh(blueprints),
            name=f"{DOMAIN}_background_refresh",
        )

    @callback
    def _async_cancel_background_task(self) -> None:
        """Cancel the background task on unload."""
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Cancelling background refresh task on unload")
            self._background_task.cancel()

    async def _async_background_refresh(self, blueprints: dict[str, Any]) -> None:
        """Fetch remote updates in the background using a task queue.

        This method initializes a pool of background workers to process
        blueprint updates concurrently. It ensures that workers are cleaned up
        gracefully by enqueuing a sentinel (None) for each worker and waiting
        for them to terminate using asyncio.gather, even if the task is canceled
        while awaiting the queue to join.

        Args:
            blueprints: Dictionary of blueprints to check for updates.

        """
        try:
            if self._refresh_lock.locked():
                _LOGGER.debug("Background refresh already running, skipping")
                return

            async with self._refresh_lock:
                self._safe_hostname_cache.clear()
                results_to_notify: list[str] = []
                updated_domains: set[str] = set()
                queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

                for path, info in blueprints.items():
                    queue.put_nowait((path, info))

                session = get_async_client(self.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)

                async def _worker() -> None:
                    """Process blueprints from the queue."""
                    while True:
                        item = await queue.get()
                        if item is None:
                            queue.task_done()
                            break

                        blueprint_path, blueprint_info = item
                        try:
                            await self._async_update_blueprint_in_place(
                                session,
                                blueprint_path,
                                blueprint_info,
                                results_to_notify,
                                updated_domains,
                            )
                            self.async_set_updated_data(self.data)
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            _LOGGER.exception(
                                "Error in background worker for %s: %s", blueprint_path, err
                            )
                        finally:
                            queue.task_done()

                workers = [
                    self.hass.async_create_background_task(_worker(), name=f"{DOMAIN}_worker_{i}")
                    for i in range(MAX_CONCURRENT_REQUESTS)
                ]

                cancelled = False
                try:
                    if workers:
                        await queue.join()
                except asyncio.CancelledError:
                    cancelled = True
                    for worker in workers:
                        worker.cancel()
                    raise
                finally:
                    if not cancelled:
                        for _ in workers:
                            await queue.put(None)
                    if workers:
                        await asyncio.gather(*workers, return_exceptions=True)

                if not queue.empty():
                    _LOGGER.warning(
                        "Background refresh finished with %d unprocessed items in queue",
                        queue.qsize(),
                    )

                _LOGGER.debug("Background refresh complete")
                await self._async_save_metadata()
                if results_to_notify:
                    await self._async_handle_notifications(results_to_notify, updated_domains)
        finally:
            self._background_task = None

    async def _async_save_metadata(self, force: bool = False, skip_filter: bool = False) -> None:
        """Save current ETags and remote hashes to persistent storage.

        We merge the newly detected ETags and hashes from self.data with
        our existing persisted maps. This ensures that metadata for
        blueprints that are currently filtered out but still exist on
        disk is not lost during the save operation.

        Args:
            force: If True, bypass equality checks and write to disk.
            skip_filter: If True, bypass os.path.isfile checks on candidate paths.

        """
        if not self.setup_complete:
            return

        merged_etags: dict[str, str] = {}
        merged_hashes: dict[str, str] = {}
        all_metadata_paths = set(self._persisted_etags.keys()) | set(self._persisted_hashes.keys())
        all_candidate_paths = all_metadata_paths | set(self.data.keys())

        for path in all_candidate_paths:
            if path in self.data:
                etag = self.data[path].get("etag")
                r_hash = self.data[path].get("remote_hash")
            else:
                etag = self._persisted_etags.get(path)
                r_hash = self._persisted_hashes.get(path)

            if etag:
                merged_etags[path] = etag
            if r_hash:
                merged_hashes[path] = r_hash

        if not skip_filter and all_candidate_paths:
            final_etags, final_hashes = await self.hass.async_add_executor_job(
                self._filter_existing_metadata, all_candidate_paths, merged_etags, merged_hashes
            )
        else:
            final_etags, final_hashes = merged_etags, merged_hashes

        if (
            not force
            and final_etags == self._persisted_etags
            and final_hashes == self._persisted_hashes
        ):
            return

        _LOGGER.debug(
            "Saving %d ETags and %d remote hashes to storage (merged)",
            len(final_etags),
            len(final_hashes),
        )
        self._persisted_etags = final_etags
        self._persisted_hashes = final_hashes
        await self._store.async_save({"etags": final_etags, "remote_hashes": final_hashes})

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and cancel tasks."""
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Cancelling background refresh task due to shutdown")
            self._background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_task
            self._background_task = None

    async def _async_handle_notifications(
        self, auto_updated_names: list[str], domains: set[str] | None = None
    ) -> None:
        """Handle services reload and persistent notifications.

        Args:
            auto_updated_names: List of blueprint names that were updated.
            domains: Set of domains affected (e.g., automation, script).

        """
        auto_updated_names.sort()
        _LOGGER.info("Auto-updated %d blueprints: %s", len(auto_updated_names), auto_updated_names)
        await self.async_reload_services(domains)

        try:
            title = await self.async_translate("auto_update_title")
            message_template = await self.async_translate("auto_update_message")

            blueprints_list = "\n".join(f"- {name}" for name in auto_updated_names)
            message = message_template.format(blueprints=blueprints_list)

            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": f"{DOMAIN}_auto_update",
                },
            )
        except Exception as err:
            _LOGGER.warning("Failed to send auto-update notification: %s", err)

    @staticmethod
    def _validate_blueprint(data: Any, source_url: str) -> str | None:
        """Validate blueprint data using HA Core's Blueprint class.

        Performs basic structure check, structural validation,
        and min_version compatibility check.

        Args:
            data: Parsed YAML dictionary of the blueprint.
            source_url: The URL the blueprint was loaded from (for logging).

        Returns:
            An error string key if validation fails, or None if valid.

        """
        if not isinstance(data, dict) or "blueprint" not in data:
            _LOGGER.warning(
                "Remote content from %s is not a valid blueprint (missing 'blueprint' key)",
                source_url,
            )
            return "invalid_blueprint"

        try:
            bp = Blueprint(data, schema=BLUEPRINT_SCHEMA)
            if errors := bp.validate():
                error_msg = "; ".join(errors)
                _LOGGER.warning(
                    "Blueprint from %s is incompatible: %s",
                    source_url,
                    error_msg,
                )
                return f"incompatible|{_sanitize_error_detail(error_msg)}"
        except InvalidBlueprint as err:
            _LOGGER.warning(
                "Blueprint validation failed for %s: %s",
                source_url,
                err,
            )
            return f"validation_error|{_sanitize_error_detail(str(err))}"
        return None

    async def async_reload_services(self, domains: list[str] | set[str] | None = None) -> None:
        """Reload specific domains or default ones if they are allowed.

        Allowed domains are limited to automation, script, and template
        to prevent malicious blueprints from triggering unintended reloads.

        Args:
            domains: List of domains to reload. If None, reloads all allowed.

        """
        if domains:
            targets = [d for d in domains if d in ALLOWED_RELOAD_DOMAINS]
        else:
            targets = list(ALLOWED_RELOAD_DOMAINS)

        for domain in targets:
            if self.hass.services.has_service(domain, "reload"):
                await self.hass.services.async_call(domain, "reload")

    async def async_fetch_blueprint(self, path: str, force: bool = False) -> None:
        """Fetch content for a single blueprint if needed.

        Args:
            path: Path to the blueprint.
            force: If True, bypass ETag and force a full download.

        """
        if not self.data or path not in self.data:
            return

        info = self.data[path]
        if not info.get("source_url"):
            return

        session = get_async_client(self.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)

        results_to_notify: list[str] = []
        updated_domains: set[str] = set()

        await self._async_update_blueprint_in_place(
            session, path, info, results_to_notify, updated_domains, force=force
        )
        self.async_set_updated_data(self.data)

    @staticmethod
    def _rotate_backups(file_path: str, max_bak: int) -> None:
        """Rotate backup files for a given file path with robust error handling.

        Args:
            file_path: Path to the active file to rotate.
            max_bak: Maximum number of backups to keep.

        """
        try:
            if not os.path.isfile(file_path):
                return

            legacy_bak = f"{file_path}.bak"
            try:
                if os.path.isfile(legacy_bak):
                    bak1 = f"{file_path}.bak.1"
                    if not os.path.isfile(bak1):
                        os.rename(legacy_bak, bak1)
                    else:
                        os.remove(legacy_bak)
            except OSError as err:
                _LOGGER.warning("Error migrating legacy backup %s: %s", legacy_bak, err)

            for i in range(max_bak, 0, -1):
                src = f"{file_path}.bak.{i}"
                dst = f"{file_path}.bak.{i + 1}"
                try:
                    os.replace(src, dst)
                except FileNotFoundError:
                    pass
                except OSError as err:
                    _LOGGER.warning("Error rotating backup %s to %s: %s", src, dst, err)

            try:
                shutil.copy2(file_path, f"{file_path}.bak.1")
            except OSError as err:
                _LOGGER.warning("Error creating new backup for %s: %s", file_path, err)

            stale_bak = f"{file_path}.bak.{max_bak + 1}"
            try:
                os.remove(stale_bak)
            except OSError as err:
                if not isinstance(err, FileNotFoundError):
                    _LOGGER.warning("Error removing stale backup %s: %s", stale_bak, err)

        except OSError as err:
            _LOGGER.error("Filesystem error during backup rotation for %s: %s", file_path, err)

    async def async_install_blueprint(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = True,
    ) -> None:
        """Install a blueprint to the local filesystem.

        Args:
            path: Target filesystem path for the blueprint.
            remote_content: Raw YAML content to write.
            reload_services: Whether to reload HA services after writing.
            backup: Whether to create backup files of the old version.

        """
        real_path = os.path.realpath(path)
        if not self._is_safe_path(real_path):
            _LOGGER.error("Security violation: Attempted to install to unsafe path: %s", real_path)
            raise HomeAssistantError(
                "Security violation: Attempted to install to an unsafe location"
            )

        if not remote_content:
            _LOGGER.error("Cannot install blueprint at %s: content is empty or None", path)
            raise HomeAssistantError("Blueprint content is missing or empty")

        max_backups = get_max_backups(self.config_entry)

        try:

            def _save_file(file_path: str, content: str, max_bak: int) -> None:
                """Local helper for _save_file."""
                tmp_path = f"{file_path}.tmp"

                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(content)

                if backup:
                    self._rotate_backups(file_path, max_bak)

                os.replace(tmp_path, file_path)

            await self.hass.async_add_executor_job(
                _save_file, real_path, remote_content, max_backups
            )

            if reload_services:
                domain = "automation"
                if bp_block := self._get_blueprint_block(path, remote_content):
                    domain = self._normalize_domain(bp_block.get("domain"))
                elif self.data and path in self.data:
                    domain = self.data[path].get("domain", "automation")
                    _LOGGER.debug(
                        "Blueprint metadata at %s is malformed; "
                        "using cached domain '%s' for reload",
                        path,
                        domain,
                    )
                else:
                    _LOGGER.info(
                        "Blueprint metadata at %s is malformed and not cached; "
                        "falling back to 'automation' domain for reload",
                        path,
                    )
                await self.async_reload_services([domain])

            if self.data and path in self.data:
                new_hash = self._hash_content(remote_content)

                self.data[path].update(
                    {
                        "updatable": False,
                        "local_hash": new_hash,
                        "remote_hash": new_hash,
                        "last_error": None,
                        "remote_content": None,
                        "invalid_remote_hash": None,
                    }
                )

                self.async_set_updated_data(self.data)
                await self._async_save_metadata(force=True)

            _LOGGER.info("Blueprint at %s updated successfully", real_path)
        except Exception as err:
            _LOGGER.error("Failed to update blueprint at %s: %s", path, err)
            raise

    async def _is_safe_url(self, url: str) -> bool:
        """Check if the URL is safe (not an internal network address).

        Args:
            url: The URL to validate.

        Returns:
            True if the URL points to a safe public hostname.

        """
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False

        hostname_lower = hostname.lower()

        if hostname_lower.rsplit(".", 1)[-1] in SPECIAL_USE_TLDS:
            return False

        if hostname_lower in self._safe_hostname_cache:
            return self._safe_hostname_cache[hostname_lower]

        async with self._safe_hostname_lock:
            if hostname_lower in self._safe_hostname_cache:
                return self._safe_hostname_cache[hostname_lower]

            result = await self._perform_safe_hostname_check(hostname_lower)
            self._safe_hostname_cache[hostname_lower] = result
            return result

    async def _perform_safe_hostname_check(self, hostname: str) -> bool:
        """Perform the actual DNS lookup and safety validation.

        Args:
            hostname: The hostname or IP to check.

        Returns:
            True if the destination is a safe public IP.

        """
        with contextlib.suppress(ValueError):
            ip = ipaddress.ip_address(hostname)
            return self._is_ip_safe(ip)

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                addr_infos = await self.hass.async_add_executor_job(
                    socket.getaddrinfo, hostname, 0, 0, 0, 0, 0
                )
            found_safe_ip = False
            for _, _, _, _, sockaddr in addr_infos:
                ip_str = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if not self._is_ip_safe(ip):
                    return False
                found_safe_ip = True
        except (TimeoutError, socket.gaierror):
            return False

        return found_safe_ip

    @staticmethod
    def _is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Check if an IP address is safe (public).

        Args:
            ip: The IP address to check.

        Returns:
            True if the IP is public and safe.

        """
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    def _is_safe_path(self, path: str) -> bool:
        """Check if the path is within the blueprints' directory.

        Args:
            path: Filesystem path to validate.

        Returns:
            True if the path is safely contained within blueprints folder.

        """
        blueprint_path = self.hass.config.path("blueprints")
        try:
            real_path = os.path.realpath(path)
            real_blueprints = os.path.realpath(blueprint_path)
            return os.path.commonpath([real_path, real_blueprints]) == real_blueprints
        except (ValueError, OSError):
            return False

    async def async_restore_blueprint(self, path: str, version: int = 1) -> dict[str, Any]:
        """Restore a blueprint from a numbered backup file.

        The current blueprint is preserved as a new backup before the
        restore, making the operation reversible.

        Args:
            path: Local path of the blueprint file to restore.
            version: Which backup version to restore (1 = newest).

        Returns:
            A dictionary with 'success' (bool) and 'translation_key' (str).

        """
        real_path = os.path.realpath(path)
        if not self._is_safe_path(real_path):
            _LOGGER.error("Security violation: Attempted to restore unsafe path: %s", real_path)
            return {"success": False, "translation_key": "system_error"}

        max_backups = get_max_backups(self.config_entry)
        if version < 1 or version > max_backups:
            _LOGGER.error(
                "Invalid backup version %s requested for %s (current limit: %s)",
                version,
                real_path,
                max_backups,
            )
            return {"success": False, "translation_key": "system_error"}

        try:

            def _restore_file(file_path: str, ver: int, max_bak: int) -> tuple[bool, str]:
                """Local helper for _restore_file."""
                bak_path = f"{file_path}.bak.{ver}"
                if ver == 1 and not os.path.isfile(bak_path):
                    old_bak = f"{file_path}.bak"
                    if os.path.isfile(old_bak):
                        bak_path = old_bak

                if not os.path.isfile(bak_path):
                    return False, "missing_backup"

                with open(bak_path, encoding="utf-8") as f:
                    content = f.read()

                tmp_path = f"{file_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(content)

                self._rotate_backups(file_path, max_bak)
                os.replace(tmp_path, file_path)
                return True, "success"

            success, message = await self.hass.async_add_executor_job(
                _restore_file, real_path, version, max_backups
            )

            if success:
                domain = "automation"
                if self.data and path in self.data:
                    domain = self.data[path].get("domain", "automation")
                await self.async_reload_services([domain])
                await self.async_request_refresh()

            return {
                "success": success,
                "translation_key": message,
            }
        except Exception as err:
            _LOGGER.error("Failed to restore blueprint at %s: %s", real_path, err)
            return {
                "success": False,
                "translation_key": "system_error",
                "translation_kwargs": {"error": str(err)},
            }

    def get_cached_git_diff(
        self, path: str, local_hash: str | None, remote_hash: str | None
    ) -> GitDiffResult | None:
        """Get cached git diff.

        Returns:
            GitDiffResult if cached, else None.
        """
        info = self.data.get(path, {})
        cached = info.get("_cached_git_diff")
        if cached and isinstance(cached, dict):
            c_local = cached.get("local")
            c_remote = cached.get("remote")
            c_diff = cached.get("diff")
            c_semantic = cached.get("semantic_sync", False)
            if local_hash == c_local and remote_hash == c_remote and isinstance(c_diff, str):
                return GitDiffResult(diff_text=c_diff, is_semantic_sync=c_semantic)
        return None

    def set_cached_git_diff(
        self,
        path: str,
        local_hash: str | None,
        remote_hash: str | None,
        diff_text: str,
        is_semantic_sync: bool = False,
    ) -> None:
        """Set cached git diff.

        Args:
            path: Local path of the blueprint.
            local_hash: Hash of the local file.
            remote_hash: Hash of the remote content.
            diff_text: Generated unified diff string.
            is_semantic_sync: Whether the diff is empty due to semantic sync.
        """
        if path in self.data:
            self.data[path]["_cached_git_diff"] = {
                "local": local_hash,
                "remote": remote_hash,
                "diff": diff_text,
                "semantic_sync": is_semantic_sync,
            }

    async def async_fetch_diff_content(self, path: str) -> str | None:
        """
        Fetch and validate remote blueprint content for generating a diff.
        
        If the blueprint at `path` is eligible and the normalized source URL is safe, fetches remote content, ensures the `source_url` is present in the content, and validates it as a blueprint. On success, stores the normalized remote content in the blueprint's `info["remote_content"]` and clears `info["last_error"]`. On failure (not eligible, unsafe URL, fetch failure, or validation error) updates the blueprint's error state (`info["last_error"]`) or calls the central error updater for unsafe URLs, and returns `None`.
        
        Returns:
            `str` containing the normalized remote content with `source_url` ensured, or `None` if the blueprint is not eligible, the URL is unsafe, fetching fails, or validation fails.
        """
        info = self.data.get(path)
        if not info or not info.get("updatable"):
            return None

        source_url = info.get("source_url", "")
        normalized_url = self._normalize_url(source_url)
        if not normalized_url:
            return None

        if not await self._is_safe_url(normalized_url):
            _LOGGER.warning("Blocking diff fetch from unsafe URL: (redacted URL)")
            self._update_error_state(path, "unsafe_url", source_url)
            return None

        session = get_async_client(self.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)
        cdn_url = self._get_cdn_url(normalized_url) if self.is_cdn_enabled() else None

        remote_content, _ = await self._async_fetch_with_cdn_fallback(
            session,
            path,
            normalized_url,
            cdn_url,
            stored_etag=None,
            stored_remote_hash=None,
            force=True,
        )

        if not remote_content:
            return None

        remote_content_with_url = self._ensure_source_url(remote_content, source_url)
        try:
            blueprint_dict = yaml_util.parse_yaml(remote_content_with_url)
            last_error = self._validate_blueprint(blueprint_dict, source_url)
        except (HomeAssistantError, InvalidBlueprint) as err:
            last_error = f"yaml_syntax_error|{_sanitize_error_detail(str(err))}"

        if last_error:
            _LOGGER.warning("Remote content for diff at %s is invalid: %s", path, last_error)
            info["last_error"] = last_error
            return None

        info["last_error"] = None
        info["remote_content"] = remote_content_with_url
        return remote_content_with_url

    async def async_get_git_diff(self, path: str) -> GitDiffResult | None:
        """
        Retrieve a unified diff between the local and remote blueprint content for the given path.
        
        May fetch remote content and update internal cached state; returns `None` if a diff cannot be produced.
        
        Returns:
            `GitDiffResult` containing the unified diff text and `is_semantic_sync` flag, or `None` if unavailable.
        """
        if path not in self.data:
            return None

        info = self.data[path]
        local_hash = info.get("local_hash")
        remote_hash = info.get("remote_hash")

        if (result := self.get_cached_git_diff(path, local_hash, remote_hash)) is not None:
            return result

        remote_content = info.get("remote_content")
        if remote_content is None and info.get("updatable"):
            try:
                remote_content = await self.async_fetch_diff_content(path)
            except Exception as err:
                _LOGGER.warning(
                    "Context fetch failed for diff at %s: %s",
                    path,
                    _sanitize_error_detail(str(err)),
                )
                return None

        if not remote_content:
            return None

        try:
            diff_text = await self.hass.async_add_executor_job(
                self._read_and_diff, path, remote_content, info.get("source_url", "")
            )
        except OSError as err:
            _LOGGER.warning("I/O error generating diff for %s: %s", path, err)
            return None
        except Exception as err:
            _LOGGER.error("Unexpected error generating diff for %s: %s", path, err, exc_info=True)
            return None

        is_semantic_sync = not (diff_text or "").strip() and self._is_semantically_equal(
            remote_content, local_hash, already_normalized=True
        )
        self.set_cached_git_diff(path, local_hash, remote_hash, diff_text or "", is_semantic_sync)
        return GitDiffResult(diff_text=diff_text or "", is_semantic_sync=is_semantic_sync)

    def is_auto_update_enabled(self) -> bool:
        """
        Determine whether blueprint auto-update is enabled.
        
        Checks the integration option `CONF_AUTO_UPDATE`, falling back to the legacy value in `config_entry.data` and then to `DEFAULT_AUTO_UPDATE` when not present or when `config_entry` is missing.
        
        Returns:
            `true` if auto-update is enabled, `false` otherwise.
        """
        if not self.config_entry:
            return DEFAULT_AUTO_UPDATE

        return self.config_entry.options.get(
            CONF_AUTO_UPDATE,
            self.config_entry.data.get(CONF_AUTO_UPDATE, DEFAULT_AUTO_UPDATE),
        )

    def is_cdn_enabled(self) -> bool:
        """
        Determine whether jsDelivr CDN should be used for fetching remote blueprints.
        
        Returns:
            `true` if CDN usage is enabled, `false` otherwise.
        """
        if not self.config_entry:
            return DEFAULT_USE_CDN

        return self.config_entry.options.get(
            CONF_USE_CDN,
            self.config_entry.data.get(CONF_USE_CDN, DEFAULT_USE_CDN),
        )

    def _update_error_state(
        self, path: str, error_type: str, detail: Any, clear_etag: bool = False
    ) -> None:
        """
        Update the stored blueprint entry to reflect a failure and record a sanitized error.
        
        If the given path exists in the coordinator's `data`, clears remote-related fields, sets `updatable` to False, resets `invalid_remote_hash`, and sets `last_error` to `"{error_type}|{sanitized_detail}"` where `sanitized_detail` redacts sensitive data and is truncated. Optionally clears the stored `etag`. If the path is not present, logs a warning and makes no state changes.
        
        Parameters:
            path (str): Local filesystem path of the blueprint to update.
            error_type (str): Short category identifier for the error (for example `"fetch_error"`).
            detail (Any): Additional error information; will be converted to a string and sanitized.
            clear_etag (bool): If True, remove the persisted ETag for this blueprint.
        """
        if self.data and path in self.data:
            update_data = {
                "remote_hash": None,
                "remote_content": None,
                "updatable": False,
                "last_error": f"{error_type}|{_sanitize_error_detail(str(detail))}",
                "invalid_remote_hash": None,
            }
            if clear_etag:
                update_data["etag"] = None

            self.data[path].update(update_data)
        else:
            _LOGGER.warning("Attempted to update error state for missing blueprint path: %s", path)

    async def _async_fetch_with_cdn_fallback(
        self,
        session: httpx.AsyncClient,
        path: str,
        normalized_url: str,
        cdn_url: str | None,
        stored_etag: str | None,
        stored_remote_hash: str | None,
        force: bool,
    ) -> tuple[str | None, str | None]:
        """
        Attempt to fetch blueprint content from a CDN and fall back to the original source if needed.
        
        Tries the provided `cdn_url` first (when present). Uses the provided `stored_etag` only when `stored_remote_hash` is present and `force` is False. If the CDN fetch returns content or a new ETag, that result is returned immediately; otherwise the function fetches from `normalized_url`.
        
        Parameters:
            path (str): Local blueprint path used for logging context.
            normalized_url (str): Canonicalized original source URL to fall back to.
            cdn_url (str | None): CDN URL to try before the original source, or None to skip CDN.
            stored_etag (str | None): Previously stored ETag to send with conditional requests.
            stored_remote_hash (str | None): Previously stored remote content hash; when absent the ETag is not used.
            force (bool): When True, ignore stored ETag and force a full download.
        
        Returns:
            tuple[str | None, str | None]: `(remote_content, new_etag)` where `remote_content` is the fetched text or `None` for a 304 Not Modified, and `new_etag` is the response ETag (or `None` if not provided).
        """
        etag = stored_etag if (stored_remote_hash and not force) else None

        if cdn_url:
            try:
                _LOGGER.debug("Fetching blueprint via CDN: (redacted URL)")
                remote_content, new_etag = await self._async_fetch_content(
                    session, cdn_url, etag=etag, force=force
                )
                if remote_content or (remote_content is None and new_etag is not None):
                    return remote_content, new_etag
            except (TimeoutError, httpx.HTTPError, HomeAssistantError) as err:
                _LOGGER.warning(
                    "CDN fetch failed for %s; falling back to original source: %s",
                    path,
                    _sanitize_error_detail(str(err)),
                )

        return await self._async_fetch_content(session, normalized_url, etag=etag, force=force)

    async def _async_update_blueprint_in_place(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: dict[str, Any],
        results_to_notify: list[str],
        updated_domains: set[str],
        force: bool = False,
    ) -> None:
        """
        Update the coordinator's metadata for a single local blueprint by fetching and processing its remote source.
        
        Parameters:
            session (httpx.AsyncClient): HTTP client used for remote requests.
            path (str): Absolute local filesystem path of the blueprint.
            info (dict[str, Any]): Current metadata entry for the blueprint (as stored in self.data).
            results_to_notify (list[str]): Mutable list to append blueprint names that triggered notifications.
            updated_domains (set[str]): Mutable set to add domains whose services should be reloaded.
            force (bool): If True, bypass ETag/conditional fetch behavior and force a full download.
        
        Behavior:
            - Validates the blueprint has a `source_url` and that the URL is safe.
            - Attempts to fetch remote content (optionally via CDN) and handles `304 Not Modified`.
            - On successful fetch, processes the remote content and updates coordinator state; may auto-install updates.
            - On failures or invalid content, updates the blueprint's error state and may clear stored ETag.
        
        """
        if not (source_url := info.get("source_url")):
            return

        if not await self._is_safe_url(source_url):
            _LOGGER.warning("Blocking update from untrusted URL: (redacted URL)")
            self._update_error_state(path, "unsafe_url", source_url, clear_etag=True)
            return

        normalized_url = self._normalize_url(source_url)
        if not normalized_url:
            return

        cdn_url = self._get_cdn_url(normalized_url) if self.is_cdn_enabled() else None

        stored_etag = self.data.get(path, {}).get("etag")
        stored_remote_hash = self.data.get(path, {}).get("remote_hash")

        try:
            remote_content, new_etag = await self._async_fetch_with_cdn_fallback(
                session,
                path,
                normalized_url,
                cdn_url,
                stored_etag,
                stored_remote_hash,
                force,
            )

            if remote_content is None:
                remote_content, new_etag = await self._handle_not_modified_case(
                    session, path, info, normalized_url, new_etag
                )
        except (TimeoutError, httpx.HTTPError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Failed to fetch blueprint from (redacted URL): %s",
                _sanitize_error_detail(str(err)),
            )
            self._update_error_state(path, "fetch_error", err)
            return

        if remote_content is None:
            return

        if remote_content == "":
            self._update_error_state(path, "empty_content", "", clear_etag=True)
            return

        try:
            await self._process_blueprint_content(
                path,
                info,
                remote_content,
                new_etag,
                source_url,
                results_to_notify,
                updated_domains,
            )
        except Exception as err:
            _LOGGER.error(
                "Error processing blueprint from (redacted URL): %s",
                _sanitize_error_detail(str(err)),
            )
            self._update_error_state(path, "processing_error", err, clear_etag=True)
            return

    async def _handle_not_modified_case(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: dict[str, Any],
        normalized_url: str,
        new_etag: str | None,
    ) -> tuple[str | None, str | None]:
        """
        Process a 304 Not Modified response for a blueprint, update stored metadata, and perform an on-demand fetch when auto-update is enabled.
        
        Updates the stored ETag (if provided) and the blueprint's `updatable` flag based on the stored `remote_hash`. If the blueprint is considered updatable and auto-update is enabled, initiates a forced fetch (using CDN when configured) and returns its result.
        
        Returns:
            tuple(content, etag): `content` is the fetched remote content when a forced fetch occurs, or `None` if no fetch was performed; `etag` is the updated ETag value or `None`.
        """
        _LOGGER.debug("[304] '%s' is up to date on server", info["name"])
        if not (self.data and path in self.data):
            return None, new_etag

        if new_etag:
            self.data[path]["etag"] = new_etag

        remote_hash = self.data[path].get("remote_hash")
        if not remote_hash:
            return None, new_etag

        local_hash = info["local_hash"]
        self.data[path]["updatable"] = local_hash != remote_hash

        if self.data[path]["updatable"] and self.is_auto_update_enabled():
            _LOGGER.debug(
                "Auto-update enabled for '%s', fetching on-demand",
                info["name"],
            )
            cdn_url = self._get_cdn_url(normalized_url) if self.is_cdn_enabled() else None
            return await self._async_fetch_with_cdn_fallback(
                session,
                path,
                normalized_url,
                cdn_url,
                stored_etag=None,
                stored_remote_hash=None,
                force=True,
            )

        return None, new_etag

    async def _process_blueprint_content(
        self,
        path: str,
        info: dict[str, Any],
        remote_content: str,
        new_etag: str | None,
        source_url: str,
        results_to_notify: list[str],
        updated_domains: set[str],
    ) -> None:
        """
        Process and validate newly fetched blueprint content and update coordinator state.
        
        Validates and canonicalizes the fetched YAML, computes its content hash, and determines whether the blueprint is updatable. If validation succeeds and auto-update is enabled, installs the blueprint and records the successful auto-update (appends the blueprint name to `results_to_notify` and adds its domain to `updated_domains`). On validation or install failure, records a sanitized `last_error`, preserves the invalid remote hash, clears remote content, and marks the blueprint not updatable. In all cases, updates `self.data[path]` (when present) with `remote_hash`, `invalid_remote_hash`, `remote_content`, `updatable`, `etag`, and `last_error` as appropriate.
        
        Parameters:
            path: Absolute local filesystem path of the blueprint.
            info: Current in-memory metadata for the blueprint (entry from `self.data` scan).
            remote_content: Raw YAML text fetched from the remote source.
            new_etag: ETag returned by the HTTP response, if any.
            source_url: Canonical source URL expected to be present in the blueprint content.
            results_to_notify: Mutable list that will receive the blueprint name when an auto-update succeeds.
            updated_domains: Mutable set that will receive the blueprint's domain when an auto-update succeeds.
        """
        remote_content = self._ensure_source_url(remote_content, source_url)
        remote_hash = self._hash_content(remote_content, already_normalized=True)
        local_hash = info["local_hash"]
        updatable = remote_hash != local_hash

        try:
            blueprint_dict = yaml_util.parse_yaml(remote_content)
            last_error = self._validate_blueprint(blueprint_dict, source_url)
        except (HomeAssistantError, InvalidBlueprint) as err:
            last_error = f"yaml_syntax_error|{_sanitize_error_detail(str(err))}"

        auto_update = self.is_auto_update_enabled()

        if updatable and not last_error and auto_update:
            try:
                await self.async_install_blueprint(
                    path, remote_content, reload_services=False, backup=True
                )
                if self.data and path in self.data:
                    self.data[path].update(
                        {
                            "remote_hash": remote_hash,
                            "remote_content": None,
                            "updatable": False,
                            "local_hash": remote_hash,
                            "last_error": None,
                            "etag": new_etag,
                        }
                    )
                results_to_notify.append(info["name"])
                updated_domains.add(info.get("domain", "automation"))
                return
            except Exception as err:
                _LOGGER.error("Auto-update failed for %s: %s", path, err)
                last_error = f"auto_update_failed|{_sanitize_error_detail(str(err))}"

        if self.data and path in self.data:
            if last_error:
                update_data = {
                    "last_error": last_error,
                    "etag": new_etag,
                    "invalid_remote_hash": remote_hash,
                    "remote_hash": None,
                    "remote_content": None,
                    "updatable": False,
                }
            else:
                update_data = {
                    "last_error": last_error,
                    "etag": new_etag,
                    "invalid_remote_hash": None,
                    "remote_hash": remote_hash,
                    "remote_content": remote_content if updatable else None,
                    "updatable": updatable,
                }

            self.data[path].update(update_data)

    @retry_async(
        max_retries=MAX_RETRIES,
        exceptions=(httpx.HTTPError, socket.gaierror, TimeoutError),
        base_delay=RETRY_BACKOFF,
    )
    async def _async_fetch_content(
        self,
        session: httpx.AsyncClient,
        url: str,
        etag: str | None = None,
        force: bool = False,
    ) -> tuple[str | None, str | None]:
        """
        Fetch the text content at a URL and return it together with the response ETag.
        
        Performs conditional requests when `etag` is provided (unless `force` is True), follows a limited number of redirects while enforcing safety checks, and supports special parsing for Home Assistant forum topic JSON responses.
        
        Parameters:
            url (str): The request URL.
            etag (str | None): Optional ETag for conditional GET; when provided and `force` is False an If-None-Match header is sent.
            force (bool): When True, do not use the provided `etag` for conditional requests and always download.
        
        Returns:
            tuple[str | None, str | None]: A tuple of (content, etag). `content` is the response text, or `None` when the server returned 304 Not Modified. `etag` is the response ETag if present.
        
        Raises:
            httpx.HTTPError: On HTTP/network errors, too many redirects, unsafe redirect targets, or invalid forum JSON responses.
        """
        headers: dict[str, str] = {}
        if etag and not force:
            headers["If-None-Match"] = etag

        async with self._pacing_lock:
            now = time.monotonic()
            interval = random.uniform(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
            start_time = max(now, self._last_request_time + interval)
            delay = start_time - now
            self._last_request_time = start_time

        if delay > 0:
            await asyncio.sleep(delay)

        _LOGGER.debug("[Pacing] Dispatching request for (redacted URL)")

        current_url = url
        current_headers = headers.copy()

        response: httpx.Response | None = None
        for redirect_count in range(21):
            response = await session.get(
                current_url,
                headers=current_headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            )

            if response.status_code == 304:
                return None, response.headers.get("ETag") or etag

            if not response.is_redirect:
                response.raise_for_status()
                break

            if redirect_count >= 20:
                _LOGGER.error("Too many redirects fetching (redacted URL)")
                raise httpx.HTTPError("Too many redirects")

            next_url = response.headers.get("Location")
            if not next_url:
                response.raise_for_status()
                break

            next_url = str(response.url.join(next_url))

            if not await self._is_safe_url(next_url):
                _LOGGER.warning("Blocking redirect to unsafe URL: (redacted URL)")
                raise httpx.HTTPError("Security violation: Redirected to unsafe URL (redacted URL)")

            current_url = next_url
            current_headers = {}

        if response is None:
            raise httpx.HTTPError("Request failed without response")

        new_etag = response.headers.get("ETag")
        parsed_url = urlparse(current_url)
        if parsed_url.hostname == DOMAIN_HA_FORUM:
            try:
                json_data = response.json()
            except ValueError as err:
                raise httpx.HTTPError(
                    f"Invalid JSON response from forum URL (redacted URL): "
                    f"{_sanitize_error_detail(str(err))}"
                ) from err
            content = self._parse_forum_content(json_data)
            return (content or ""), new_etag

        content = response.text
        return content, new_etag

    @staticmethod
    def _normalize_url(url: str) -> str:
        """
        Convert GitHub, Gist, or Home Assistant forum links into direct/raw endpoints where applicable.
        
        If the URL points to a GitHub blob, converts it to the raw.githubusercontent.com form; if it points to a Gist, appends `/raw` when needed; if it points to a Home Assistant forum topic, converts it to the topic JSON endpoint. Returns the original URL when no conversion applies.
        
        Returns:
            The normalized URL suitable for direct content fetching, or the original URL if no transformation was performed.
        """
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if parsed.hostname == DOMAIN_GITHUB and RE_GITHUB_BLOB.search(parsed.path):
            new_parts = [p for p in path_parts if p != "blob"]
            return urlunparse(
                (
                    parsed.scheme,
                    DOMAIN_GITHUB_RAW,
                    "/".join(new_parts),
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )

        if parsed.hostname == DOMAIN_GIST and not RE_GIST_RAW.search(parsed.path):
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"{parsed.path.rstrip('/')}/raw",
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )

        if (
            parsed.hostname == DOMAIN_HA_FORUM
            and "/t/" in parsed.path
            and (match := RE_FORUM_TOPIC_ID.search(parsed.path))
        ):
            topic_id = match.group(1)
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"/t/{topic_id}.json",
                    parsed.params,
                    "",
                    parsed.fragment,
                )
            )

        return url

    @staticmethod
    def _get_cdn_url(url: str) -> str | None:
        """
        Generate a jsDelivr CDN URL for supported GitHub source URLs.
        
        Supports raw.githubusercontent.com and github.com URLs that use the `blob` or `raw` path form. Preserves any percent-encoded path segments from the input.
        
        Parameters:
            url (str): The GitHub source URL to convert.
        
        Returns:
            str | None: A jsDelivr CDN URL targeting the same file, or `None` if the input URL is not a supported GitHub format.
        """
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]

        if parsed.hostname == DOMAIN_GITHUB_RAW:
            if len(path_parts) < 4:
                return None
            user, repo, branch = path_parts[:3]
            path = "/".join(path_parts[3:])
        elif parsed.hostname == DOMAIN_GITHUB:
            if len(path_parts) < 5:
                return None

            try:
                anchor_idx = next(
                    (idx for idx, part in enumerate(path_parts) if part in ("blob", "raw")),
                    -1,
                )
                if anchor_idx != 2:
                    return None

                user = path_parts[anchor_idx - 2]
                repo = path_parts[anchor_idx - 1]
                branch = path_parts[anchor_idx + 1]
                path = "/".join(path_parts[anchor_idx + 2 :])
            except (IndexError, ValueError):
                return None
        else:
            return None

        return urlunparse(
            (
                "https",
                DOMAIN_JSDELIVR,
                f"/gh/{user}/{repo}@{branch}/{path}",
                "",
                "",
                "",
            )
        )

    @staticmethod
    def _parse_forum_content(json_data: dict[str, Any]) -> str | None:
        """
        Extract the first code block containing a Home Assistant blueprint from a Discourse topic JSON payload.
        
        Parameters:
            json_data (dict[str, Any]): JSON returned by the Discourse topic endpoint (for example `/t/{id}.json`), expected to contain `post_stream.posts[0].cooked`.
        
        Returns:
            str | None: The first code block whose unescaped text contains "blueprint:", trimmed of surrounding whitespace, or `None` if no such block is found.
        """
        with contextlib.suppress(KeyError, IndexError):
            post_stream: dict[str, Any] = json_data.get("post_stream", {})
            posts: list[dict[str, Any]] = post_stream.get("posts", [])
            if not posts:
                return None

            post_content = posts[0].get("cooked")
            if not isinstance(post_content, str):
                return None

            code_blocks: list[str] = RE_FORUM_CODE_BLOCK.findall(post_content)
            for block in code_blocks:
                unquoted_block: str = str(html.unescape(block).strip())
                if "blueprint:" in unquoted_block:
                    return unquoted_block
        return None

    @staticmethod
    def _normalize_content(content: str) -> str:
        r"""Normalize blueprint content for consistent hashing.

        This method performs transport-level normalization to ensure that
        identical files produce consistent hashes across different operating
        systems (Windows vs Linux) and transport layers. It avoids modifying
        content inside the file (such as stripping trailing spaces) to
        preserve the integrity of YAML block scalars.

        It performs the following transformations:
        1. Strips UTF-8 Byte Order Mark (BOM).
        2. Normalizes all line endings to Unix style (\n).

        Args:
            content: Raw YAML content string.

        Returns:
            Normalized YAML content.

        """
        if content.startswith("\ufeff"):
            content = content[1:]

        return content.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _hash_content(content: str, already_normalized: bool = False) -> str:
        """Centralized helper to compute a SHA256 hash with normalization.

        This ensures that we always apply transport-level normalization
        before computing the hash, preventing inconsistencies between
        different parts of the coordinator.

        Args:
            content: Raw YAML content to hash.
            already_normalized: If True, skip normalization (optimization).

        Returns:
            The hex digest of the normalized content's hash.

        """
        if already_normalized:
            normalized = content
        else:
            normalized = BlueprintUpdateCoordinator._normalize_content(content)
        return hashlib.sha256(normalized.encode()).hexdigest()

    @staticmethod
    def _ensure_source_url(content: str, source_url: str) -> str:
        """Ensure a source_url is present in the blueprint metadata.

        Parses the YAML to check for ``blueprint.source_url``. If a valid
        source_url already exists (even if formatted differently), the
        original content is returned (but still canonicalized). Otherwise,
        the URL is injected after the ``blueprint:`` key via deterministic
        text substitution.

        IMPORTANT: This function always performs transport-level canonical
        normalization (BOM removal and line ending sync) as a side effect.

        Args:
            content: Raw YAML blueprint content.
            source_url: Fallback URL to inject when the content has none.

        Returns:
            The YAML content with a source_url guaranteed to be present
            in the blueprint block, and always in canonical form.

        """
        try:
            parsed = yaml_util.parse_yaml(content)
        except HomeAssistantError:
            parsed = None

        if isinstance(parsed, dict) and "blueprint" in parsed:
            blueprint = parsed["blueprint"]
            if not isinstance(blueprint, dict):
                blueprint = parsed["blueprint"] = {}

            existing = blueprint.get("source_url")
            if isinstance(existing, str) and existing.strip():
                return BlueprintUpdateCoordinator._normalize_content(content)

        content = BlueprintUpdateCoordinator._normalize_content(content)
        source_url = source_url.strip()

        if RE_BLUEPRINT_KEY.search(content):
            return RE_BLUEPRINT_KEY.sub(
                rf"\1\n  source_url: {source_url}",
                content,
                count=1,
            )

        if isinstance(parsed, dict) and "blueprint" in parsed:
            blueprint = parsed["blueprint"]
            if not isinstance(blueprint, dict):
                blueprint = parsed["blueprint"] = {}
            blueprint["source_url"] = source_url
            try:
                return yaml_util.dump(parsed)
            except Exception as err:
                _LOGGER.warning("Structured YAML injection failed for %s: %s", source_url, err)

        return content

    @staticmethod
    def _read_and_diff(local_path: str, remote_text: str, source_url: str) -> str:
        """Read and diff local vs remote content with normalization.

        Args:
            local_path: Path to the local blueprint file.
            remote_text: Raw remote content fetched from Git.
            source_url: The source URL to ensure is present in the remote.

        Returns:
            A unified diff string.

        """
        with open(local_path, encoding="utf-8") as f:
            local_text = f.read()

        local_text = BlueprintUpdateCoordinator._normalize_content(local_text)
        remote_text = BlueprintUpdateCoordinator._ensure_source_url(remote_text, source_url)

        local_lines = local_text.splitlines(keepends=True)
        remote_lines = remote_text.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                local_lines,
                remote_lines,
                fromfile="local",
                tofile="remote",
            )
        )

    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        """Normalize and validate the blueprint domain, defaulting to 'automation'.

        Args:
            domain: The domain to normalize.

        Returns:
            The normalized lowercase domain string.

        """
        if isinstance(domain, str):
            norm_domain = domain.strip().lower()
            if norm_domain in ALLOWED_RELOAD_DOMAINS:
                return norm_domain

        if domain and str(domain).strip():
            _LOGGER.warning(
                "Unsupported or unknown blueprint domain '%s' encountered; "
                "falling back to 'automation'. Supported: %s",
                domain,
                ", ".join(ALLOWED_RELOAD_DOMAINS),
            )

        return "automation"

    @staticmethod
    def _should_include_blueprint(rel_path: str, filter_mode: str, selected_set: set[str]) -> bool:
        """Check if a blueprint should be included based on filtering rules."""
        if filter_mode == FILTER_MODE_BLACKLIST:
            return rel_path not in selected_set

        if filter_mode == FILTER_MODE_WHITELIST:
            return rel_path in selected_set

        return True

    @staticmethod
    def _get_validated_filter_mode(filter_mode: Any) -> str:
        """Normalize and validate filter mode.

        Args:
            filter_mode: The filter mode to validate.

        Returns:
            A valid filter mode (FILTER_MODE_ALL as fallback).

        """
        if not isinstance(filter_mode, str):
            if filter_mode is not None:
                _LOGGER.warning(
                    "Invalid filter mode type '%s'; falling back to all", type(filter_mode).__name__
                )
            return FILTER_MODE_ALL

        normalized_mode = filter_mode.strip().lower()
        if normalized_mode in (FILTER_MODE_ALL, FILTER_MODE_WHITELIST, FILTER_MODE_BLACKLIST):
            return normalized_mode

        _LOGGER.warning("Invalid filter mode '%s' in config; falling back to all", filter_mode)
        return FILTER_MODE_ALL

    @staticmethod
    def _get_validated_selected_blueprints(selected: Any) -> list[str]:
        """Validate and coerce selected blueprints into a list of strings.

        Args:
            selected: The selection value to validate.

        Returns:
            A valid list of blueprint paths.

        """
        if selected is None:
            return []

        if isinstance(selected, str):
            stripped = selected.strip()
            return [stripped] if stripped else []

        if isinstance(selected, (list, tuple)):
            return [str(item).strip() for item in selected if item and str(item).strip()]

        if isinstance(selected, dict):
            _LOGGER.error(
                "Invalid type for selected blueprints: mapping (%s) provided; "
                "expected string or sequence of strings. Ignoring value.",
                type(selected).__name__,
            )
            return []

        _LOGGER.error(
            "Invalid type for selected blueprints: %s; expected string or sequence of strings. "
            "Ignoring value.",
            type(selected).__name__,
        )
        return []

    @staticmethod
    def _get_blueprint_block(path: str, content: str) -> dict[str, Any] | None:
        """Extract the 'blueprint' metadata block from YAML content."""
        try:
            blueprint_dict = yaml_util.parse_yaml(content)
        except HomeAssistantError as err:
            _LOGGER.warning("Failed to parse blueprint at %s: %s", path, err)
            return None

        if not isinstance(blueprint_dict, dict):
            _LOGGER.debug(
                "Skipping blueprint at %s: parsed YAML is not a mapping (got %s)",
                path,
                type(blueprint_dict).__name__,
            )
            return None

        if "blueprint" not in blueprint_dict:
            _LOGGER.debug(
                "Skipping blueprint at %s: missing top-level 'blueprint' key",
                path,
            )
            return None

        bp_info = blueprint_dict["blueprint"]
        if not isinstance(bp_info, dict):
            _LOGGER.debug(
                "Skipping blueprint at %s: 'blueprint' key is not a mapping (got %s)",
                path,
                type(bp_info).__name__,
            )
            return None

        return bp_info

    @staticmethod
    def _parse_blueprint_data(path: str, content: str) -> ParsedBlueprintData | None:
        """Parse raw YAML content and extract blueprint metadata if valid."""
        bp_info = BlueprintUpdateCoordinator._get_blueprint_block(path, content)
        if bp_info is None:
            return None

        source_url = bp_info.get("source_url")
        if not isinstance(source_url, str) or not source_url.strip():
            _LOGGER.debug(
                "Skipping blueprint at %s: missing or empty 'source_url' in blueprint metadata",
                path,
            )
            return None

        raw_name = bp_info.get("name")
        name = (
            raw_name.strip()
            if isinstance(raw_name, str) and raw_name.strip()
            else os.path.basename(path)
        )
        domain = BlueprintUpdateCoordinator._normalize_domain(bp_info.get("domain"))

        return {
            "name": name,
            "domain": domain,
            "source_url": source_url.strip(),
            "local_hash": BlueprintUpdateCoordinator._hash_content(content),
        }

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: str,
        selected_blueprints: list[str],
    ) -> dict[str, BlueprintMetadata]:
        """Scan the blueprints directory for YAML files with source_url."""
        blueprint_path: str = hass.config.path("blueprints")
        found_blueprints = {}

        if not os.path.isdir(blueprint_path):
            _LOGGER.debug("Blueprints directory not found: %s", blueprint_path)
            return found_blueprints

        _LOGGER.debug("Scanning blueprints in: %s", blueprint_path)
        selected_set = set(selected_blueprints)

        for root, _, files in os.walk(blueprint_path):
            for file in files:
                if not file.endswith(".yaml"):
                    continue

                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, blueprint_path).replace("\\", "/")

                if not BlueprintUpdateCoordinator._should_include_blueprint(
                    rel_path, filter_mode, selected_set
                ):
                    continue

                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()

                    if parsed_data := BlueprintUpdateCoordinator._parse_blueprint_data(
                        full_path, content
                    ):
                        found_blueprints[full_path] = {
                            **parsed_data,
                            "rel_path": rel_path,
                        }

                except OSError as err:
                    _LOGGER.error("Error reading blueprint at %s: %s", full_path, err)

        return found_blueprints
