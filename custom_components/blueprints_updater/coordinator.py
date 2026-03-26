from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import shutil
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import yaml as yaml_util

from .const import (
    CONCURRENT_REQUESTS_LIMIT,
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
    MAX_RETRIES,
    RE_BLUEPRINT_KEY,
    RE_FORUM_CODE_BLOCK,
    RE_FORUM_TOPIC_ID,
    RE_GIST_RAW,
    RE_GITHUB_BLOB,
    RE_SOURCE_URL_LINE,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
)
from .utils import retry_async

_LOGGER = logging.getLogger(__name__)


class BlueprintUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching blueprint updates."""

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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch blueprint update data."""
        filter_mode = (
            self.config_entry.options.get(CONF_FILTER_MODE, FILTER_MODE_ALL)
            if self.config_entry
            else FILTER_MODE_ALL
        )
        selected_blueprints = (
            self.config_entry.options.get(CONF_SELECTED_BLUEPRINTS, []) if self.config_entry else []
        )

        _LOGGER.debug(
            "Starting blueprint update check (filter_mode=%s, selected_blueprints=%s)",
            filter_mode,
            selected_blueprints,
        )
        blueprints = await self.hass.async_add_executor_job(
            self.scan_blueprints,
            self.hass,
            filter_mode,
            selected_blueprints,
        )

        results: dict[str, Any] = {
            path: {
                "name": info["name"],
                "rel_path": info["rel_path"],
                "source_url": info["source_url"],
                "local_hash": info["hash"],
                "updatable": False,
                "remote_hash": None,
                "remote_content": None,
                "last_error": None,
            }
            for path, info in blueprints.items()
        }

        semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS_LIMIT)

        async with aiohttp.ClientSession() as session:
            tasks = [
                self._async_update_blueprint(session, semaphore, path, info, results)
                for path, info in blueprints.items()
            ]
            await asyncio.gather(*tasks)

        auto_updated_count = sum(1 for info in results.values() if info.pop("_auto_updated", False))
        if auto_updated_count > 0:
            _LOGGER.info("Auto-updated %d blueprints", auto_updated_count)
            await self.async_reload_services()

        return results

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
            return "Invalid blueprint: Missing 'blueprint' root key"

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
                return f"Incompatible: {error_msg}"
        except Exception as err:
            _LOGGER.warning(
                "Blueprint validation failed for %s: %s",
                source_url,
                err,
            )
            return f"Validation Error: {err}"
        return None

    async def async_reload_services(self) -> None:
        """Reload automation, script, and template services."""
        for domain in ("automation", "script", "template"):
            if self.hass.services.has_service(domain, "reload"):
                await self.hass.services.async_call(domain, "reload")

    async def async_install_blueprint(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = False,
    ) -> None:
        """Install a blueprint by overwriting the local file atomically.

        Args:
            path: Local path of the blueprint file.
            remote_content: The new YAML content to write.
            reload_services: Whether to reload HA services after writing.
            backup: If True, creates rotating numbered backups before overwriting.
        """
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
                await self.async_reload_services()

            if getattr(self, "data", None) and path in self.data:
                self.data[path]["updatable"] = False

            _LOGGER.info("Blueprint at %s updated successfully", path)
        except Exception as err:
            _LOGGER.error("Failed to update blueprint at %s: %s", path, err)
            raise

    async def async_restore_blueprint(self, path: str, version: int = 1) -> dict[str, Any]:
        """Restore a blueprint from a numbered backup file.

        Args:
            path: Local path of the blueprint file to restore.
            version: Which backup version to restore (1 = newest).

        Returns:
            A dictionary with 'success' (bool) and 'translation_key' (str).
        """
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
                await self.async_reload_services()
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

    async def _async_update_blueprint(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        path: str,
        info: dict[str, Any],
        results: dict[str, Any],
    ) -> None:
        """Update a single blueprint."""
        source_url = info.get("source_url")
        if not source_url:
            _LOGGER.debug("Skipping blueprint at %s: no source_url defined", path)
            return

        _LOGGER.debug("Checking for updates: %s (source: %s)", info["name"], source_url)
        normalized_url = self._normalize_url(source_url)
        _LOGGER.debug("Normalized URL for %s: %s", source_url, normalized_url)

        async with semaphore:
            try:
                remote_content = await self._async_fetch_content(session, normalized_url)

                _LOGGER.debug("Fetched %d bytes from %s", len(remote_content or ""), normalized_url)

                if not remote_content:
                    _LOGGER.warning("Empty content received from %s", normalized_url)
                    results[path]["last_error"] = "Empty content received"
                    return

                remote_content = self._ensure_source_url(remote_content, source_url)

                remote_hash = hashlib.sha256(remote_content.encode()).hexdigest()
                local_hash = info["hash"]
                updatable = remote_hash != local_hash

                try:
                    data = yaml_util.parse_yaml(remote_content)
                    last_error = self._validate_blueprint(data, source_url)
                except Exception as err:
                    _LOGGER.warning(
                        "Could not parse remote blueprint from %s: %s",
                        source_url,
                        err,
                    )
                    last_error = f"YAML Syntax Error: {err}"

                if (
                    updatable
                    and not last_error
                    and self.config_entry
                    and self.config_entry.options.get(CONF_AUTO_UPDATE, False)
                ):
                    await self.async_install_blueprint(
                        path, remote_content, reload_services=False, backup=True
                    )
                    results[path].update(
                        {
                            "remote_hash": remote_hash,
                            "remote_content": None,
                            "updatable": False,
                            "local_hash": remote_hash,
                            "last_error": None,
                            "_auto_updated": True,
                        }
                    )
                    return

                results[path].update(
                    {
                        "remote_hash": remote_hash,
                        "remote_content": remote_content if updatable and not last_error else None,
                        "updatable": updatable,
                        "last_error": last_error,
                    }
                )
            except Exception as err:
                _LOGGER.error("Error fetching blueprint after retries from %s: %s", source_url, err)
                results[path]["last_error"] = f"Fetch Error: {err}"

    @retry_async(max_retries=MAX_RETRIES, base_delay=RETRY_BACKOFF)
    async def _async_fetch_content(self, session: aiohttp.ClientSession, url: str) -> str:
        """Fetch content from a URL with retries."""
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()

            if DOMAIN_HA_FORUM in url:
                json_data = await response.json()
                return self._parse_forum_content(json_data) or ""

            return await response.text()

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
        """Extract YAML blueprint from Discourse JSON response."""
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
                    data = yaml_util.parse_yaml(content)

                    if isinstance(data, dict) and "blueprint" in data:
                        bp_info = data["blueprint"]
                        source_url = bp_info.get("source_url")
                        if source_url:
                            found_blueprints[full_path] = {
                                "name": bp_info.get("name", file),
                                "rel_path": rel_path,
                                "source_url": source_url,
                                "hash": hashlib.sha256(content.encode()).hexdigest(),
                            }
                except Exception as err:
                    _LOGGER.error("Error reading blueprint at %s: %s", full_path, err)

        return found_blueprints
