"""Data coordinator for Blueprints Updater."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import ipaddress
import logging
import os
import random
import shutil
import socket
import time
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import httpx
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
    DOMAIN,
    DOMAIN_GIST,
    DOMAIN_GITHUB,
    DOMAIN_GITHUB_RAW,
    DOMAIN_HA_FORUM,
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
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    SPECIAL_USE_TLDS,
    STORAGE_KEY_DATA,
    STORAGE_VERSION,
)
from .utils import get_max_backups, retry_async

_LOGGER = logging.getLogger(__name__)


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

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch blueprint update data.

        This method performs a fast local scan and returns immediate results
        to ensure the integration starts instantly. Remote updates are
        triggered in a background task.

        Returns:
            A dictionary containing blueprint information and update status.

        """
        filter_mode = (
            self.config_entry.options.get(CONF_FILTER_MODE, FILTER_MODE_ALL)
            if self.config_entry
            else FILTER_MODE_ALL
        )
        selected_blueprints = (
            self.config_entry.options.get(CONF_SELECTED_BLUEPRINTS, []) if self.config_entry else []
        )

        _LOGGER.debug(
            "Starting fast local blueprint scan (filter_mode=%s)",
            filter_mode,
        )

        blueprints = await self.hass.async_add_executor_job(
            self.scan_blueprints,
            self.hass,
            filter_mode,
            selected_blueprints,
        )

        scanned_paths = set(blueprints.keys())
        old_keys_count = len(self._persisted_etags) + len(self._persisted_hashes)

        self._persisted_etags = {
            path: etag for path, etag in self._persisted_etags.items() if path in scanned_paths
        }
        self._persisted_hashes = {
            path: r_hash for path, r_hash in self._persisted_hashes.items() if path in scanned_paths
        }

        if (len(self._persisted_etags) + len(self._persisted_hashes)) < old_keys_count:
            _LOGGER.debug("Pruned stale blueprint metadata from memory, triggering save")
            self.hass.async_create_background_task(
                self._async_save_metadata(), name=f"{DOMAIN}_prune_save"
            )

        results: dict[str, dict[str, Any]] = {
            path: {
                "name": info["name"],
                "rel_path": info["rel_path"],
                "domain": info["domain"],
                "source_url": info["source_url"],
                "local_hash": info["local_hash"],
                "updatable": False,
                "remote_hash": None if self.data else self._persisted_hashes.get(path),
                "invalid_remote_hash": None,
                "remote_content": None,
                "last_error": None,
                "etag": None if self.data else self._persisted_etags.get(path),
            }
            for path, info in blueprints.items()
        }

        for info in results.values():
            if info.get("remote_hash"):
                info["updatable"] = info["local_hash"] != info["remote_hash"]

        if self.data:
            for path, info in results.items():
                if path in self.data and self.data[path]["local_hash"] == info["local_hash"]:
                    info.update(
                        {
                            "updatable": self.data[path]["updatable"],
                            "remote_hash": self.data[path]["remote_hash"],
                            "invalid_remote_hash": self.data[path].get("invalid_remote_hash"),
                            "remote_content": self.data[path]["remote_content"],
                            "last_error": self.data[path]["last_error"],
                            "etag": self.data[path].get("etag"),
                        }
                    )

        self.data = results
        self._start_background_refresh(blueprints)

        _LOGGER.debug("Instant setup complete with %d blueprints", len(results))
        return results

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

    async def _async_save_metadata(self) -> None:
        """Save current ETags and remote hashes to persistent storage."""
        if not self.setup_complete:
            return

        etags = {
            path: cast(str, info["etag"]) for path, info in self.data.items() if info.get("etag")
        }
        hashes = {
            path: cast(str, info["remote_hash"])
            for path, info in self.data.items()
            if info.get("remote_hash")
        }

        if etags == self._persisted_etags and hashes == self._persisted_hashes:
            return

        _LOGGER.debug("Saving %d ETags and %d remote hashes to storage", len(etags), len(hashes))
        self._persisted_etags = etags
        self._persisted_hashes = hashes
        await self._store.async_save({"etags": etags, "remote_hashes": hashes})

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
                return f"incompatible|{error_msg}"
        except Exception as err:
            _LOGGER.warning(
                "Blueprint validation failed for %s: %s",
                source_url,
                err,
            )
            return f"validation_error|{err}"
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
                try:
                    blueprint_dict = yaml_util.parse_yaml(remote_content)
                    if isinstance(blueprint_dict, dict) and "blueprint" in blueprint_dict:
                        domain = blueprint_dict["blueprint"].get("domain", "automation")
                except HomeAssistantError as err:
                    _LOGGER.warning("Failed to parse blueprint at %s: %s", path, err)
                await self.async_reload_services([domain])

            if self.data and path in self.data:
                new_hash = hashlib.sha256(remote_content.encode()).hexdigest()
                self.data[path].update(
                    {
                        "updatable": False,
                        "local_hash": new_hash,
                        "last_error": None,
                        "remote_content": None,
                    }
                )
                if self.data[path].get("remote_hash") == new_hash:
                    self.data[path]["invalid_remote_hash"] = None

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

    async def _async_update_blueprint_in_place(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: dict[str, Any],
        results_to_notify: list[str],
        updated_domains: set[str],
        force: bool = False,
    ) -> None:
        """Update a single blueprint directly in self.data.

        Args:
            session: Async HTTP client session.
            path: Local path of the blueprint.
            info: Current blueprint metadata.
            results_to_notify: List of names for notification.
            updated_domains: Set of domains affected.
            force: If True, ignore ETag and force a full download.

        """
        if not (source_url := info.get("source_url")):
            return

        if not await self._is_safe_url(source_url):
            _LOGGER.warning("Blocking update from untrusted URL: %s", source_url)
            if self.data and path in self.data:
                self.data[path].update(
                    {
                        "remote_hash": None,
                        "remote_content": None,
                        "updatable": False,
                        "last_error": "unsafe_url|",
                        "etag": None,
                    }
                )
            return

        normalized_url = self._normalize_url(source_url)

        stored_etag = self.data.get(path, {}).get("etag")
        stored_remote_hash = self.data.get(path, {}).get("remote_hash")

        try:
            remote_content, new_etag = await self._async_fetch_content(
                session,
                normalized_url,
                etag=stored_etag if (stored_remote_hash and not force) else None,
                force=force,
            )

            if remote_content is None:
                remote_content, new_etag = await self._handle_not_modified_case(
                    session, path, info, normalized_url, new_etag
                )

            if remote_content is None:
                return

            if remote_content == "":
                if self.data and path in self.data:
                    self.data[path].update(
                        {
                            "last_error": "empty_content|",
                            "remote_hash": None,
                            "remote_content": None,
                            "updatable": False,
                            "invalid_remote_hash": None,
                        }
                    )
                return

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
            _LOGGER.error("Error fetching blueprint from %s: %s", source_url, err)
            if self.data and path in self.data:
                self.data[path].update(
                    {
                        "last_error": f"fetch_error|{err}",
                        "remote_hash": None,
                        "remote_content": None,
                        "updatable": False,
                    }
                )

    async def _handle_not_modified_case(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: dict[str, Any],
        normalized_url: str,
        new_etag: str | None,
    ) -> tuple[str | None, str | None]:
        """Handle the 304 Not Modified case for a blueprint.

        Args:
            session: Async HTTP client session.
            path: Local path of the blueprint.
            info: Current blueprint metadata.
            normalized_url: The URL used to fetch.
            new_etag: The ETag returned (if any).

        Returns:
            A tuple of (content, etag). Content is None if still not modified.

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

        if (
            self.data[path]["updatable"]
            and self.config_entry
            and self.config_entry.options.get(CONF_AUTO_UPDATE, False)
        ):
            _LOGGER.debug(
                "Auto-update enabled for '%s', fetching on-demand",
                info["name"],
            )
            return await self._async_fetch_content(session, normalized_url, force=True)

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
        """Process and validate newly fetched blueprint content.

        Args:
            path: Local path of the blueprint.
            info: Current blueprint metadata.
            remote_content: Raw YAML content.
            new_etag: ETag from response.
            source_url: Original source URL.
            results_to_notify: List to track auto-updates for notification.
            updated_domains: Set to track domains requiring reload.

        """
        remote_content = self._ensure_source_url(remote_content, source_url)
        remote_hash = hashlib.sha256(remote_content.encode()).hexdigest()
        local_hash = info["local_hash"]
        updatable = remote_hash != local_hash

        try:
            blueprint_dict = yaml_util.parse_yaml(remote_content)
            last_error = self._validate_blueprint(blueprint_dict, source_url)
        except HomeAssistantError as err:
            last_error = f"yaml_syntax_error|{err}"

        auto_update = self.config_entry and self.config_entry.options.get(CONF_AUTO_UPDATE, False)

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
                last_error = f"auto_update_failed|{err}"

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
        """Fetch content from a URL.

        Returns (content, etag). Content is None on 304 Not Modified.

        Args:
            session: Async HTTP client.
            url: URL to fetch.
            etag: Optional ETag for conditional GET.
            force: If True, bypass ETag (even if provided) and force download.

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

        _LOGGER.debug("[Pacing] Dispatching request for %s", url)

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
                _LOGGER.error("Too many redirects fetching %s", url)
                raise httpx.HTTPError("Too many redirects")

            next_url = response.headers.get("Location")
            if not next_url:
                response.raise_for_status()
                break

            next_url = str(response.url.join(next_url))

            if not await self._is_safe_url(next_url):
                _LOGGER.warning("Blocking redirect to unsafe URL: %s", next_url)
                raise httpx.HTTPError(f"Security violation: Redirected to unsafe URL {next_url}")

            current_url = next_url
            current_headers = {}

        if response is None:
            raise httpx.HTTPError("Request failed without response")

        new_etag = response.headers.get("ETag")
        parsed_url = urlparse(current_url)
        if DOMAIN_HA_FORUM in parsed_url.netloc:
            json_data = response.json()
            content = self._parse_forum_content(json_data)
            return (content or ""), new_etag

        content = response.text
        return content, new_etag

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Convert standard GitHub/Gist/Forum URLs to their raw/API endpoints.

        Args:
            url: The user-provided source URL.

        Returns:
            The normalized URL for direct content fetching.

        """
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if parsed.netloc == DOMAIN_GITHUB and RE_GITHUB_BLOB.search(parsed.path):
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

        if parsed.netloc == DOMAIN_GIST and not RE_GIST_RAW.search(parsed.path):
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
            DOMAIN_HA_FORUM in parsed.netloc
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
    def _parse_forum_content(json_data: dict[str, Any]) -> str | None:
        """Extract YAML blueprint from Home Assistant Forum JSON response.

        Args:
            json_data: The JSON payload from the Discourse API.

        Returns:
            The extracted blueprint YAML string or None if not found.

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
    def _ensure_source_url(content: str, source_url: str) -> str:
        """Ensure a source_url is present in the blueprint metadata.

        Parses the YAML to check for ``blueprint.source_url`` (matching
        the same lookup used by ``scan_blueprints``). If a valid
        source_url already exists, the content is returned unchanged.
        Otherwise, the fallback URL is injected after the ``blueprint:``
        key via text substitution.

        Args:
            content: Raw YAML blueprint content.
            source_url: Fallback URL to inject when the content has none.

        Returns:
            The YAML content with a source_url guaranteed to be present
            in the blueprint block.

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
                return content

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
    def _should_include_blueprint(rel_path: str, filter_mode: str, selected_set: set[str]) -> bool:
        """Check if a blueprint should be included based on filtering rules."""
        if filter_mode == FILTER_MODE_BLACKLIST:
            return rel_path not in selected_set

        if filter_mode == FILTER_MODE_WHITELIST:
            return rel_path in selected_set

        if filter_mode == FILTER_MODE_ALL:
            return True

        _LOGGER.warning(
            "Unknown blueprint filter_mode '%s' for '%s'; excluding from scan",
            filter_mode,
            rel_path,
        )
        return False

    @staticmethod
    def _parse_blueprint_data(path: str, content: str) -> dict[str, Any] | None:
        """Parse raw YAML content and extract blueprint metadata if valid."""
        try:
            blueprint_dict = yaml_util.parse_yaml(content)
        except HomeAssistantError as err:
            _LOGGER.warning("Failed to parse blueprint at %s: %s", path, err)
            return None

        if not isinstance(blueprint_dict, dict) or "blueprint" not in blueprint_dict:
            return None

        bp_info = blueprint_dict["blueprint"]
        if not isinstance(bp_info, dict):
            return None

        source_url = bp_info.get("source_url")
        if not isinstance(source_url, str) or not source_url.strip():
            return None

        return {
            "name": bp_info.get("name", os.path.basename(path)),
            "domain": bp_info.get("domain", "automation"),
            "source_url": source_url.strip(),
            "local_hash": hashlib.sha256(content.encode()).hexdigest(),
        }

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: str,
        selected_blueprints: list[str],
    ) -> dict[str, Any]:
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

                    if metadata := BlueprintUpdateCoordinator._parse_blueprint_data(
                        full_path, content
                    ):
                        metadata["rel_path"] = rel_path
                        found_blueprints[full_path] = metadata

                except Exception as err:
                    _LOGGER.error("Error reading blueprint at %s: %s", full_path, err)

        return found_blueprints
