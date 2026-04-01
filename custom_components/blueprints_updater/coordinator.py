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
from homeassistant.core import HomeAssistant
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
    CONF_MAX_BACKUPS,
    CONF_SELECTED_BLUEPRINTS,
    DEFAULT_MAX_BACKUPS,
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
    RE_SOURCE_URL_LINE,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    SPECIAL_USE_TLDS,
    STORAGE_KEY_DATA,
    STORAGE_VERSION,
)
from .utils import retry_async

_LOGGER = logging.getLogger(__name__)


class BlueprintUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Class to manage fetching blueprint updates."""

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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self._translations: dict[tuple[str, str], dict[str, str]] = {}
        self._translation_lock = asyncio.Lock()
        self.setup_complete = False
        self._background_task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._pacing_lock = asyncio.Lock()
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DATA)
        self._persisted_etags: dict[str, str] = {}
        self._persisted_hashes: dict[str, str] = {}
        self._safe_hostname_cache: dict[str, bool] = {}
        self._safe_hostname_lock = asyncio.Lock()

    async def async_setup(self) -> None:
        """Load persisted data."""
        storage_data = await self._store.async_load()
        if storage_data and isinstance(storage_data, dict):
            self._persisted_etags = storage_data.get("etags", {})
            self._persisted_hashes = storage_data.get("remote_hashes", {})
            _LOGGER.debug(
                "Loaded %d persisted ETags and %d remote hashes",
                len(self._persisted_etags),
                len(self._persisted_hashes),
            )

    async def async_translate(self, key: str, category: str = "common", **kwargs: Any) -> str:
        """Translate a key using the current language and category.

        This method is a wrapper around async_get_translations that provides
        a more convenient API and better error handling for startup race conditions.
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
                    except Exception as err:
                        _LOGGER.debug(
                            "Could not load translations for %s (%s) for language %s: %s",
                            DOMAIN,
                            category,
                            language,
                            err,
                        )
                        self._translations[cache_key] = {}

        translations = self._translations.get(cache_key, {})
        full_key = f"component.{DOMAIN}.{category}.{key}"
        template = translations.get(f"{full_key}.message") or translations.get(full_key, key)

        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, ValueError, IndexError) as err:
            _LOGGER.debug(
                "Error formatting translation for key %s in category %s: %s",
                key,
                category,
                err,
            )
            return template

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch blueprint update data.

        This method performs a fast local scan and returns immediate results
        to ensure the integration starts instantly. Remote updates are
        triggered in a background task.
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

        results: dict[str, dict[str, Any]] = {
            path: {
                "name": info["name"],
                "rel_path": info["rel_path"],
                "domain": info["domain"],
                "source_url": info["source_url"],
                "local_hash": info["hash"],
                "updatable": False,
                "remote_hash": self._persisted_hashes.get(path) if not self.data else None,
                "remote_content": None,
                "last_error": None,
                "etag": self._persisted_etags.get(path) if not self.data else None,
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
        """Start the background remote refresh task if not already running."""
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Background refresh already in progress, skipping start")
            return

        self._background_task = self.hass.async_create_background_task(
            self._async_background_refresh(blueprints),
            name=f"{DOMAIN}_background_refresh",
        )

    async def _async_background_refresh(self, blueprints: dict[str, Any]) -> None:
        """Fetch remote updates in the background using a task queue."""
        try:
            if self._refresh_lock.locked():
                _LOGGER.debug("Background refresh already running, skipping")
                return

            async with self._refresh_lock:
                self._safe_hostname_cache.clear()
                results_to_notify: list[str] = []
                updated_domains: set[str] = set()
                queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

                for path, info in blueprints.items():
                    queue.put_nowait((path, info))

                session = get_async_client(self.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)

                async def _worker() -> None:
                    """Process blueprints from the queue."""
                    while True:
                        try:
                            blueprint_path, blueprint_info = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                        try:
                            await self._async_update_blueprint_in_place(
                                session,
                                blueprint_path,
                                blueprint_info,
                                results_to_notify,
                                updated_domains,
                            )
                            self.async_set_updated_data(self.data)
                        except Exception as err:
                            _LOGGER.error(
                                "Error in background worker for %s: %s", blueprint_path, err
                            )
                        finally:
                            queue.task_done()

                workers = [
                    self.hass.async_create_background_task(_worker(), name=f"{DOMAIN}_worker_{i}")
                    for i in range(MAX_CONCURRENT_REQUESTS)
                ]

                if workers:
                    await asyncio.gather(*workers)

                _LOGGER.debug("Background refresh complete")
                await self._async_save_metadata()
                if results_to_notify:
                    await self._async_handle_notifications(results_to_notify, updated_domains)
        finally:
            self._background_task = None

    async def _async_save_metadata(self) -> None:
        """Save current ETags and remote hashes to persistent storage."""
        if not self.data:
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
        """Handle services reload and persistent notifications."""
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
        Returns an error string if validation fails, or None if valid.
        """
        if not isinstance(data, dict) or "blueprint" not in data:
            _LOGGER.warning(
                "Remote content from %s is not a valid blueprint (missing 'blueprint' key)",
                source_url,
            )
            return "invalid_blueprint"

        try:
            bp = Blueprint(data, schema=BLUEPRINT_SCHEMA)
            errors = bp.validate()
            if errors:
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
        """
        if domains:
            targets = [d for d in domains if d in ALLOWED_RELOAD_DOMAINS]
        else:
            targets = list(ALLOWED_RELOAD_DOMAINS)

        for domain in targets:
            if self.hass.services.has_service(domain, "reload"):
                await self.hass.services.async_call(domain, "reload")

    async def async_fetch_blueprint(self, path: str) -> None:
        """Fetch content for a single blueprint if needed."""
        if not self.data or path not in self.data:
            return

        info = self.data[path]
        if not info.get("source_url"):
            return

        session = get_async_client(self.hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)
        results_to_notify: list[str] = []
        updated_domains: set[str] = set()

        await self._async_update_blueprint_in_place(
            session, path, info, results_to_notify, updated_domains
        )
        self.async_set_updated_data(self.data)

    async def async_install_blueprint(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = True,
    ) -> None:
        """Install a blueprint to the local filesystem."""
        if not self._is_safe_path(path):
            _LOGGER.error("Security violation: Attempted to install to unsafe path: %s", path)
            return

        if not remote_content:
            _LOGGER.error("Cannot install blueprint at %s: content is empty or None", path)
            raise HomeAssistantError("Blueprint content is missing or empty")

        max_backups = DEFAULT_MAX_BACKUPS
        if self.config_entry:
            max_backups = self.config_entry.options.get(CONF_MAX_BACKUPS, DEFAULT_MAX_BACKUPS)

        try:

            def _save_file(file_path: str, content: str, max_bak: int) -> None:
                tmp_path = f"{file_path}.tmp"

                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(content)

                if backup and os.path.exists(file_path):
                    old_bak = f"{file_path}.bak"
                    if os.path.exists(old_bak):
                        os.replace(old_bak, f"{file_path}.bak.1")
                    for i in range(max_bak, 1, -1):
                        src = f"{file_path}.bak.{i - 1}"
                        dst = f"{file_path}.bak.{i}"
                        if os.path.exists(src):
                            os.replace(src, dst)

                    shutil.copy2(file_path, f"{file_path}.bak.1")

                    old = f"{file_path}.bak.{max_bak + 1}"
                    if os.path.exists(old):
                        os.remove(old)

                os.replace(tmp_path, file_path)

            await self.hass.async_add_executor_job(_save_file, path, remote_content, max_backups)

            if reload_services:
                domain = "automation"
                try:
                    blueprint_dict = yaml_util.parse_yaml(remote_content)
                    if isinstance(blueprint_dict, dict) and "blueprint" in blueprint_dict:
                        domain = blueprint_dict["blueprint"].get("domain", "automation")
                except Exception as err:
                    _LOGGER.warning("Failed to parse blueprint at %s: %s", path, err)
                    pass
                await self.async_reload_services([domain])

            if self.data and path in self.data:
                self.data[path]["updatable"] = False

            _LOGGER.info("Blueprint at %s updated successfully", path)
        except Exception as err:
            _LOGGER.error("Failed to update blueprint at %s: %s", path, err)
            raise

    async def _is_safe_url(self, url: str) -> bool:
        """Check if the URL is safe (not an internal network address)."""
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
        """Perform the actual DNS lookup and safety validation."""
        try:
            ip = ipaddress.ip_address(hostname)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local)
        except ValueError:
            pass

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                addr_infos = await self.hass.async_add_executor_job(
                    socket.getaddrinfo, hostname, 0, 0, 0, 0, 0
                )
            for _, _, _, _, sockaddr in addr_infos:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return False
        except (socket.gaierror, ValueError, TimeoutError):
            return False

        return True

    def _is_safe_path(self, path: str) -> bool:
        """Check if the path is within the blueprints' directory."""
        blueprint_path = self.hass.config.path("blueprints")
        try:
            abs_path = str(os.path.abspath(path))
            abs_blueprints = str(os.path.abspath(blueprint_path))
            return os.path.commonpath([abs_path, abs_blueprints]) == abs_blueprints
        except (ValueError, OSError):
            return False

    async def async_restore_blueprint(self, path: str, version: int = 1) -> dict[str, Any]:
        """Restore a blueprint from a numbered backup file.

        Args:
            path: Local path of the blueprint file to restore.
            version: Which backup version to restore (1 = newest).

        Returns:
            A dictionary with 'success' (bool) and 'translation_key' (str).
        """
        if not self._is_safe_path(path):
            _LOGGER.error("Security violation: Attempted to restore unsafe path: %s", path)
            return {"success": False, "translation_key": "system_error"}

        try:

            def _restore_file(file_path: str, ver: int) -> tuple[bool, str]:
                bak_path = f"{file_path}.bak.{ver}"
                if ver == 1 and not os.path.exists(bak_path):
                    old_bak = f"{file_path}.bak"
                    if os.path.exists(old_bak):
                        bak_path = old_bak
                if not os.path.exists(bak_path):
                    return False, "missing_backup"
                os.replace(bak_path, file_path)
                return True, "success"

            success, message = await self.hass.async_add_executor_job(_restore_file, path, version)

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
            _LOGGER.error("Failed to restore blueprint at %s: %s", path, err)
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
    ) -> None:
        """Update a single blueprint directly in self.data."""
        source_url: str | None = info.get("source_url")
        if not source_url:
            return

        if not await self._is_safe_url(source_url):
            _LOGGER.warning("Blocking update from untrusted URL: %s", source_url)
            return

        normalized_url = self._normalize_url(source_url)
        if self.data is None:
            self.data = {}
        stored_etag = self.data.get(path, {}).get("etag")
        stored_remote_hash = self.data.get(path, {}).get("remote_hash")

        try:
            remote_content, new_etag = await self._async_fetch_content(
                session, normalized_url, etag=stored_etag if stored_remote_hash else None
            )

            if remote_content is None:
                _LOGGER.debug("[304] '%s' is up to date on server", info["name"])
                if self.data and path in self.data:
                    if new_etag:
                        self.data[path]["etag"] = new_etag
                    remote_hash = self.data[path].get("remote_hash")
                    if remote_hash:
                        self.data[path]["updatable"] = info["hash"] != remote_hash
                return

            if not remote_content:
                if self.data and path in self.data:
                    self.data[path]["last_error"] = "empty_content"
                return

            remote_content = self._ensure_source_url(remote_content, source_url)
            remote_hash = hashlib.sha256(remote_content.encode()).hexdigest()
            local_hash = info["hash"]
            updatable = remote_hash != local_hash

            try:
                blueprint_dict = yaml_util.parse_yaml(remote_content)
                last_error = self._validate_blueprint(blueprint_dict, source_url)
            except Exception as err:
                last_error = f"yaml_syntax_error|{err}"

            if (
                updatable
                and not last_error
                and self.config_entry
                and self.config_entry.options.get(CONF_AUTO_UPDATE, False)
            ):
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

            if self.data and path in self.data:
                self.data[path].update(
                    {
                        "remote_hash": remote_hash,
                        "remote_content": remote_content if updatable and not last_error else None,
                        "updatable": updatable,
                        "last_error": last_error,
                        "etag": new_etag,
                    }
                )
        except Exception as err:
            _LOGGER.error("Error fetching blueprint from %s: %s", source_url, err)
            if self.data and path in self.data:
                self.data[path]["last_error"] = f"fetch_error|{err}"

    @retry_async(max_retries=MAX_RETRIES, base_delay=RETRY_BACKOFF)
    async def _async_fetch_content(
        self,
        session: httpx.AsyncClient,
        url: str,
        etag: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Fetch content from a URL.

        Returns (content, etag). Content is None on 304 Not Modified.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag

        async with self._pacing_lock:
            now = time.monotonic()
            interval = random.uniform(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
            delay = max(0.0, (self._last_request_time + interval) - now)
            self._last_request_time = now + delay

        if delay > 0:
            await asyncio.sleep(delay)

        _LOGGER.debug("[Pacing] Dispatching request for %s", url)

        response = await session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        new_etag = response.headers.get("ETag")

        if response.status_code == 304:
            return None, etag if etag else new_etag

        response.raise_for_status()

        if DOMAIN_HA_FORUM in url:
            json_data = response.json()
            content = self._parse_forum_content(json_data)
            return (content or ""), new_etag

        content = response.text
        return content, new_etag

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Convert standard URLs to raw/API URLs."""
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

        if DOMAIN_HA_FORUM in parsed.netloc and "/t/" in parsed.path:
            match = RE_FORUM_TOPIC_ID.search(parsed.path)
            if match:
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
        """Extract YAML blueprint from Home Assistant Forum JSON response."""
        try:
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
        except (KeyError, IndexError):
            pass
        return None

    @staticmethod
    def _ensure_source_url(content: str, source_url: str) -> str:
        """Ensure the source_url is present in the blueprint section."""
        for match in RE_SOURCE_URL_LINE.finditer(content):
            if match.group(1) == source_url:
                return content

        return RE_BLUEPRINT_KEY.sub(
            rf"\1\n  source_url: {source_url}",
            content,
            count=1,
        )

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: str,
        selected_blueprints: list[str],
    ) -> dict[str, Any]:
        """Scan the blueprints directory for YAML files with source_url.

        Args:
            hass: HomeAssistant instance.
            filter_mode: Blueprint filter mode.
            selected_blueprints: List of selected blueprints.

        Returns:
            Dictionary mapping paths to blueprint properties.
        """
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

                if filter_mode == FILTER_MODE_BLACKLIST and rel_path in selected_set:
                    continue
                if filter_mode == FILTER_MODE_WHITELIST and rel_path not in selected_set:
                    continue

                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()
                    blueprint_dict = yaml_util.parse_yaml(content)

                    if isinstance(blueprint_dict, dict) and "blueprint" in blueprint_dict:
                        bp_info = blueprint_dict["blueprint"]
                        source_url = bp_info.get("source_url")
                        if source_url:
                            found_blueprints[full_path] = {
                                "name": bp_info.get("name", file),
                                "rel_path": rel_path,
                                "domain": bp_info.get("domain", "automation"),
                                "source_url": source_url,
                                "hash": hashlib.sha256(content.encode()).hexdigest(),
                            }
                except Exception as err:
                    _LOGGER.error("Error reading blueprint at %s: %s", full_path, err)

        return found_blueprints
