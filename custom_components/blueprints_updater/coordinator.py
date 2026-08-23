"""Data coordinator for Blueprints Updater."""

import asyncio
import contextlib
import difflib
import hashlib
import logging
import os
import random
import socket
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import Self, TypedDict
from urllib.parse import urlparse

import httpx
import orjson
import voluptuous as vol
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.automation.config import (
    async_validate_config_item as async_validate_automation_config,
)
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.components.script.config import (
    async_validate_config_item as async_validate_script_config,
)
from homeassistant.components.template.config import (
    async_validate_config_section as async_validate_template_config,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, TemplateError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.selector import validate_selector
from homeassistant.helpers.storage import Store
from homeassistant.helpers.template import Template, is_template_string
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
from homeassistant.util import yaml as yaml_util
from homeassistant.util.yaml.objects import Input

try:
    from homeassistant.components.template.config import (
        TEMPLATE_BLUEPRINT_SCHEMA,
    )
except ImportError:
    TEMPLATE_BLUEPRINT_SCHEMA: object = None

try:
    from homeassistant.util.ssl import SSL_ALPN_HTTP11_HTTP2
except ImportError:
    SSL_ALPN_HTTP11_HTTP2 = None

from .const import (
    ALLOWED_RELOAD_DOMAINS,
    ALLOWED_YAML_MIME_TYPES,
    BLUEPRINTS_DATA_DIR,
    CONF_AUTO_UPDATE,
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_MAX_BACKUPS,
    DOMAIN,
    EVENT_BLUEPRINTS_UPDATER_UPDATED,
    MAX_CONCURRENT_REQUESTS,
    MAX_RESPONSE_BYTES,
    MAX_RETRIES,
    MAX_SEND_INTERVAL,
    METADATA_STORAGE_FIELDS,
    MIN_SEND_INTERVAL,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    RISK_TYPE_TRANSLATIONS,
    STORAGE_KEY_DATA,
    STORAGE_VERSION,
    BlueprintBlockingReason,
    BlueprintRiskType,
    FilterMode,
    FunctionalDomain,
    RepairIssueType,
    SourceProviderType,
)
from .exceptions import (
    BlueprintFetchPolicyError,
    BlueprintRefreshObsoleteError,
    BlueprintRestoreValidationError,
    FileRevisionMismatchError,
)
from .file_store import (
    BlueprintFileStore,
    FileRevisionPrecondition,
    FileTransactionResult,
)
from .network import (
    async_resolve_public_addresses,
    get_guarded_async_client,
    is_special_use_hostname,
    normalize_hostname,
)
from .providers import registry
from .utils import (
    format_error_message,
    get_blueprint_relative_path,
    get_blueprint_usage_entities,
    get_config_bool,
    get_max_backups,
    get_validated_filter_mode,
    get_validated_selected_blueprints,
    normalize_domain,
    normalize_url,
    read_local_file,
    redact_url,
    retry_async,
    sanitize_error_detail,
    should_include_blueprint,
    split_error_message,
    verify_https_enforcement,
)

_LOGGER = logging.getLogger(__name__)

JSONDict = Mapping[str, "JSONValue"]
JSONList = Sequence["JSONValue"]
JSONValue = None | bool | int | float | str | JSONDict | JSONList


class StructuredRisk(TypedDict):
    """Structured breaking change risk.

    The ``args`` field must be JSON-serializable, as it is used for
    deduplication and logging. Typical shapes include e.g.
    ``{"input": "<string>"}`` or ``{"entity": "<entity_id>", "error": "<string>"}``.
    """

    type: BlueprintRiskType
    args: JSONDict


class BlueprintUpdateEventPayload(TypedDict):
    """Payload for the blueprint update event."""

    blueprint_name: str
    domain: str
    relative_path: str
    source_url: str | None
    previous_hash: str | None
    new_hash: str
    is_auto_update: bool
    had_breaking_risks: bool


class ParsedBlueprintData(TypedDict):
    """Data extracted from a blueprint YAML file."""

    name: str
    domain: str
    source_url: str
    local_hash: str
    local_file_hash: str


class BlueprintMetadata(ParsedBlueprintData):
    """Augmented blueprint data from file scanning."""

    relative_path: str
    backups_count: int


class BlueprintInfo(TypedDict, total=False):
    """Coordinator state entry for a blueprint."""

    name: str
    relative_path: str
    domain: str
    source_url: str | None
    local_hash: str
    local_file_hash: str
    updatable: bool
    remote_hash: str | None
    invalid_remote_hash: str | None
    remote_content: str | None
    last_error: str | None
    etag: str | None
    last_modified: str | None
    persisted_source_url: str | None
    backups_count: int
    update_blocking_reason: str | None
    breaking_risks: list[StructuredRisk]
    reload_pending: bool
    auto_update_last_error: str | None
    _cached_git_diff: dict[str, object]
    provider_type: str | None


@dataclass(frozen=True)
class GitDiffResult:
    """Structure for git diff generation results."""

    diff_text: str
    is_semantic_sync: bool


@dataclass(frozen=True)
class BlueprintScanContext:
    """Context and configuration for a blueprint scan operation."""

    hass: HomeAssistant
    real_blueprint_path: str
    filter_mode: FilterMode
    selected_set: set[str]
    max_backups: int


@dataclass(frozen=True)
class RefreshWorkItem:
    """Immutable ownership token for work spawned by one local scan."""

    generation: int
    path: str
    source_url: object
    local_hash: object
    local_file_hash: object
    relative_path: object

    @classmethod
    def capture(
        cls,
        generation: int,
        path: str,
        info: Mapping[str, object],
    ) -> Self:
        """Capture the fields that make queued refresh work authoritative."""
        return cls(
            generation=generation,
            path=path,
            source_url=info.get("source_url"),
            local_hash=info.get("local_hash"),
            local_file_hash=info.get("local_file_hash"),
            relative_path=info.get("relative_path"),
        )

    def matches(
        self,
        generation: int,
        data: Mapping[str, Mapping[str, object]],
    ) -> bool:
        """Return whether coordinator state still owns this work item."""
        current = data.get(self.path)
        return bool(
            self.generation == generation
            and current
            and current.get("source_url") == self.source_url
            and current.get("local_hash") == self.local_hash
            and current.get("local_file_hash") == self.local_file_hash
            and current.get("relative_path") == self.relative_path
        )


@dataclass(frozen=True)
class PreparedBlueprintInstall:
    """Validated inputs and resolved metadata for one file installation."""

    real_path: str
    parsed: dict[str, object] | None
    blueprint_block: dict[str, object] | None
    functional_domain: str
    current: dict[str, object] | None
    name: str
    source_url: str | None
    relative_path: str
    content: str


@dataclass(frozen=True)
class PreparedBlueprintRestore:
    """Validated backup content and identity for one restoration."""

    real_path: str
    content: str
    domain: str
    tracked_source_url: object
    precondition: FileRevisionPrecondition


MAX_HOSTNAME_CACHE_SIZE = 1024
"""Maximum number of entries in the safe hostname cache per refresh cycle."""

TOP_LEVEL_SELECTOR_PRESENTATION_KEYS = frozenset({"name", "description", "label", "help"})
"""Selector presentation keys excluded from compatibility comparisons."""

_LOCAL_REVISION_MISMATCH_ERROR = "Local blueprint changed; refresh and retry the update"
_RESTORE_REVISION_MISMATCH = "revision_mismatch"


class BlueprintUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, object]]]):
    """Class to manage fetching blueprint updates."""

    _client_kwargs_cache: dict[str, tuple[str, ...] | None] | None = None

    @staticmethod
    def generate_unique_id(entry_id: str, relative_path: str) -> str:
        """Generate a deterministic unique ID from an entry ID and a blueprint's relative path.

        Args:
            entry_id: The config entry ID.
            relative_path: The blueprint's relative path.

        Returns:
            The generated unique ID.

        """
        combined = f"{entry_id}_{relative_path}"
        return f"blueprint_{hashlib.sha256(combined.encode()).hexdigest()}"

    config_entry: ConfigEntry

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

        Note: After initialization, self.data is always a dictionary and is
        never None, ensuring callers can rely on dictionary semantics.

        """
        self.hass = hass
        self.config_entry = entry
        self.setup_complete = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.data: dict[str, dict[str, object]] = {}
        self._translations: dict[tuple[str, str], dict[str, str]] = {}
        self._translation_index: dict[str, dict[str, str]] = {}
        self._indexed_translation_keys: set[tuple[str, str]] = set()
        self.hass.data.setdefault(DOMAIN, {}).setdefault("translation_cache", {})
        self._translation_lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None
        self._refresh_generation = 0
        self._refresh_lock = asyncio.Lock()
        self._last_request_times: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DATA)
        self._persisted_metadata: dict[str, dict[str, object]] = {}
        self._pending_reload_domains: set[str] = set()
        self._persisted_pending_reload_domains: set[str] = set()
        self._reload_lock = asyncio.Lock()
        self._safe_hostname_cache: dict[str, bool] = {}
        self._max_hostname_cache_size = MAX_HOSTNAME_CACHE_SIZE
        self._safe_hostname_lock = asyncio.Lock()
        self._blueprint_validate_lock = asyncio.Lock()
        self._file_store = BlueprintFileStore()
        self._first_update_done = False
        if self.config_entry:
            self.config_entry.async_on_unload(self._async_cancel_background_task)

    async def async_wait_until_done(self) -> None:
        """Wait for any pending background refresh tasks to complete."""
        if self._background_task and not self._background_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._background_task

    def clear_translations(self) -> None:
        """Clear the internal translation cache.

        This method resets the coordinator's translation dictionary, allowing
        it to be re-populated on the next translation request.
        """
        _LOGGER.debug("Clearing translations for Blueprints Updater coordinator")
        self._translations = {}
        self._translation_index = {}
        self._indexed_translation_keys = set()

    @staticmethod
    def _build_translation_index(loaded: dict[str, str], language: str) -> dict[str, str]:
        """Build a flat key→template index from loaded translations.

        Extracts translation keys from all categories by stripping the
        ``component.{DOMAIN}.{category}.`` prefix (and optional ``.message``
        suffix), yielding a direct O(1) lookup dictionary.

        Args:
            loaded: Raw translation dict from async_get_translations.
            language: Language code (for debug logging).

        Returns:
            A dict mapping translation keys to their template strings.

        """
        index: dict[str, str] = {}
        prefix = f"component.{DOMAIN}."
        for full_key, template in loaded.items():
            if not isinstance(template, str) or not full_key.startswith(prefix):
                continue
            suffix = full_key[len(prefix) :]
            parts = suffix.split(".", 1)
            if len(parts) != 2:
                continue
            category = parts[0]
            key = parts[1]
            if key.endswith(".message"):
                key = key[:-8]
            if key not in index:
                index[key] = template
            else:
                _LOGGER.warning(
                    "Translation key collision for language=%s: key=%r "
                    "already indexed (previous=%r, new=%r in category=%r). "
                    "First-loaded value wins; rename one of the keys to "
                    "avoid ambiguity.",
                    language,
                    key,
                    index[key],
                    template,
                    category,
                )
        _LOGGER.debug(
            "Built translation index for %s with %d keys",
            language,
            len(index),
        )
        return index

    async def async_setup(self) -> None:
        """Load persisted data from storage.

        This method reads the stored metadata from the local filesystem
        to restore the state between restarts.
        """
        storage_data = await self._store.async_load()
        self.setup_complete = True

        if not storage_data or not isinstance(storage_data, dict):
            return

        metadata = storage_data.get("metadata") or {}
        if not isinstance(metadata, dict):
            _LOGGER.warning("Malformed metadata storage found, starting fresh")
            metadata = {}

        validated_metadata: dict[str, dict[str, object]] = {}
        for relative_path, entry in metadata.items():
            if isinstance(relative_path, str) and (
                validated := BlueprintUpdateCoordinator._validate_metadata_entry(entry)
            ):
                validated_metadata[relative_path] = validated
            else:
                _LOGGER.warning("Skipping malformed metadata entry for %s", relative_path)

        self._persisted_metadata = validated_metadata
        pending_reload_domains = storage_data.get("pending_reload_domains") or []
        if isinstance(pending_reload_domains, list):
            self._pending_reload_domains = {
                domain
                for domain in pending_reload_domains
                if isinstance(domain, str) and domain in ALLOWED_RELOAD_DOMAINS
            }
            self._persisted_pending_reload_domains = set(self._pending_reload_domains)

        _LOGGER.debug(
            "Loaded metadata for %d blueprints from storage",
            len(self._persisted_metadata),
        )

    async def async_translate(self, key: str, category: str = "common", **kwargs: object) -> str:
        """Translate a key using the current language and category.

        This method builds a unified translation index across all loaded
        categories so every lookup is a single O(1) dict access.  The index
        is built and read inside the translation lock to prevent races
        between concurrent loaders for the same language.

        A tracking set (``_indexed_translation_keys``) eliminates redundant
        per-call index rebuilds: each ``(language, category)`` entry is
        indexed exactly once, on first access.

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
                    except (OSError, ValueError) as err:
                        _LOGGER.debug(
                            "Could not load translations for %s (%s) for language %s: %s",
                            DOMAIN,
                            category,
                            language,
                            err,
                        )
                        self._translations[cache_key] = {}

                loaded = self._translations[cache_key]
                if loaded and cache_key not in self._indexed_translation_keys:
                    language_index = self._translation_index.setdefault(language, {})
                    language_index.update(self._build_translation_index(loaded, language))
                    self._indexed_translation_keys.add(cache_key)
                    _LOGGER.debug(
                        "Successfully loaded translations for language: %s, category: %s",
                        language,
                        category,
                    )

        language_index = self._translation_index.setdefault(language, {})
        for (lang, _cat), loaded in self._translations.items():
            cache = (lang, _cat)
            if lang == language and loaded and cache not in self._indexed_translation_keys:
                language_index.update(self._build_translation_index(loaded, language))
                self._indexed_translation_keys.add(cache)

        template = language_index.get(key)
        if not template:
            template = key
            _LOGGER.debug("Translation key not found: %s", key)

        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, ValueError, IndexError) as err:
            _LOGGER.debug(
                "Error formatting translation for key %s: %s",
                key,
                err,
            )
            return template

    def _get_scan_config(self) -> tuple[FilterMode, list[str]]:
        """Extract and validate filtering configuration from the entry.

        Returns:
            A tuple of (filter_mode, selected_blueprints).

        """
        filter_mode = get_validated_filter_mode(
            self.config_entry.options.get(CONF_FILTER_MODE, FilterMode.ALL)
            if self.config_entry
            else FilterMode.ALL
        )
        selected_blueprints = get_validated_selected_blueprints(
            self.config_entry.options.get(CONF_SELECTED_BLUEPRINTS, []) if self.config_entry else []
        )
        return filter_mode, selected_blueprints

    def _filter_existing_metadata(
        self, root: str, metadata: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        """Filter metadata to only include paths that exist on disk.

        This is a synchronous method intended to be run in an executor.

        Args:
            root: Absolute path to the blueprints directory.
            metadata: Map of relative path to metadata dictionary.

        Returns:
            A filtered metadata dictionary.

        """
        filtered: dict[str, dict[str, object]] = {}
        for relative_path, data in metadata.items():
            abs_path = os.path.join(root, relative_path)
            if os.path.isfile(abs_path) and (
                get_blueprint_relative_path(self.hass, abs_path) == relative_path
            ):
                filtered[relative_path] = data
            else:
                _LOGGER.warning("Invalid or unsafe blueprint path filtered: %s", relative_path)
        return filtered

    @staticmethod
    def _validate_metadata_entry(entry: object) -> dict[str, object] | None:
        """Validate and normalize a single metadata entry.

        Args:
            entry: Raw entry from storage.

        Returns:
            A cleaned metadata dictionary if valid, else None.

        """
        if not isinstance(entry, dict):
            return None

        validated: dict[str, object] = {}
        for field in METADATA_STORAGE_FIELDS:
            val = entry.get(field)
            if val is None or isinstance(val, str):
                validated[field] = val
            else:
                return None
        return validated

    async def _async_prune_stale_metadata(self, scanned_paths: set[str]) -> None:
        """Remove metadata for blueprints that no longer exist on disk.

        This method synchronizes in-memory metadata with the latest scan
        results. We preserve metadata for any path that either returned
        in the current scan or still exists as a file on the disk.

        To prevent blocking the event loop, file existence checks are
        performed in the executor.

        Args:
            scanned_paths: Set of absolute paths found on disk during
                the latest scan.

        """
        old_count = len(self._persisted_metadata)
        blueprints_root = self.hass.config.path(BLUEPRINTS_DATA_DIR)

        scanned_relative_paths: set[str] = set()
        for path in scanned_paths:
            try:
                if rel := get_blueprint_relative_path(self.hass, path):
                    scanned_relative_paths.add(rel)
            except (ValueError, TypeError, OSError):
                continue

        if (
            candidate_relative_paths := set(self._persisted_metadata.keys())
            - scanned_relative_paths
        ):
            metadata_to_check = {
                k: v for k, v in self._persisted_metadata.items() if k in candidate_relative_paths
            }
            valid_metadata = await self.hass.async_add_executor_job(
                self._filter_existing_metadata, blueprints_root, metadata_to_check
            )

            removed_relative_paths = candidate_relative_paths - set(valid_metadata.keys())

            if removed_relative_paths:
                for rel in removed_relative_paths:
                    abs_path = os.path.join(blueprints_root, rel)
                    self.data.pop(abs_path, None)
                    meta = metadata_to_check.get(rel) or {}
                    domain_obj = meta.get("domain")
                    domain = normalize_domain(domain_obj) if domain_obj else None
                    self._async_delete_withdrawn_issue_by_relative_path(rel, domain)

            self._persisted_metadata = {
                k: v for k, v in self._persisted_metadata.items() if k not in removed_relative_paths
            }

        if len(self._persisted_metadata) < old_count:
            _LOGGER.debug("Pruned stale blueprint metadata from memory, triggering save")
            self.hass.async_create_background_task(
                self._async_save_metadata(force=True), name=f"{DOMAIN}_prune_save"
            )

    async def _async_initialize_results(
        self, blueprints: dict[str, BlueprintMetadata]
    ) -> dict[str, dict[str, object]]:
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
        results = {}
        for path, info in blueprints.items():
            relative_path = info["relative_path"]
            persisted = self._persisted_metadata.get(relative_path) or {}

            results[path] = {
                "name": info["name"],
                "relative_path": relative_path,
                "domain": normalize_domain(info["domain"]),
                "source_url": info["source_url"],
                "local_hash": info["local_hash"],
                "local_file_hash": info.get("local_file_hash", info["local_hash"]),
                "updatable": False,
                "remote_hash": None if self._first_update_done else persisted.get("remote_hash"),
                "invalid_remote_hash": None,
                "remote_content": None,
                "last_error": None,
                "etag": None if self._first_update_done else persisted.get("etag"),
                "last_modified": self.data.get(path, {}).get("last_modified")
                if self._first_update_done
                else persisted.get("last_modified"),
                "persisted_source_url": persisted.get("source_url"),
                "backups_count": info.get("backups_count", 0),
            }
        return results

    def _merge_previous_data(self, results: dict[str, dict[str, object]]) -> None:
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
            for path, info in results.items():
                prev = self._handle_source_url_change(
                    path, info, info, prev_url=info.get("persisted_source_url")
                )
                if prev is not info:
                    info.update(prev)
                elif remote_hash := info.get("remote_hash"):
                    is_mismatch = info["local_hash"] != remote_hash
                    info["updatable"] = is_mismatch
                    if is_mismatch:
                        info["etag"] = None
                        info["last_modified"] = None
            return

        for path, info in results.items():
            if path in self.data and isinstance(self.data[path], dict):
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
                        "last_modified": prev.get("last_modified"),
                        "update_blocking_reason": prev.get("update_blocking_reason")
                        if is_updatable
                        else None,
                        "backups_count": info.get("backups_count", prev.get("backups_count", 0)),
                        "breaking_risks": prev.get("breaking_risks", []) if is_updatable else [],
                    }
                )

    async def _async_update_data(self) -> dict[str, dict[str, object]]:
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

        try:
            max_backups = get_max_backups(self.config_entry)
            blueprints = await self.hass.async_add_executor_job(
                self.scan_blueprints,
                self.hass,
                filter_mode,
                selected,
                max_backups,
            )
        except Exception as err:
            _LOGGER.exception("Blueprint scan failed: %s", err)
            raise HomeAssistantError(f"Blueprint scan failed: {err}") from err

        results = await self._async_initialize_results(blueprints)
        self._merge_previous_data(results)

        self._refresh_generation += 1
        generation = self._refresh_generation
        self.data = results
        self._first_update_done = True
        self._mark_pending_reload_state()
        self._start_background_refresh(blueprints, generation)

        _LOGGER.debug("Instant setup complete with %d blueprints", len(results))
        return results

    def _is_semantically_equal(self, content: object, target_hash: object, source_url: str) -> bool:
        """Check if content is semantically equal to a target hash.

        Args:
            content: Raw or normalized YAML content to compare.
            target_hash: Hash of the target blueprint (from normalized YAML).
            source_url: The source URL to use for identity-aware hashing.

        Returns:
            True if the content matches the target hash, False otherwise.

        """
        if not isinstance(content, str) or not isinstance(target_hash, str):
            return False

        try:
            content_hash = self._hash_content(content, source_url)
        except (ValueError, TypeError, HomeAssistantError) as err:
            _LOGGER.debug("Semantic comparison failed: %s", err)
            return False

        return content_hash == target_hash

    def _handle_source_url_change(
        self,
        path: str,
        info: dict[str, object],
        prev: dict[str, object] | None,
        prev_url: object = None,
    ) -> dict[str, object]:
        """Handle detected change in blueprint source URL.

        If the URL changed, invalidate all remote-derived metadata and trigger
        an immediate save to prevent stale state reuse after a restart.

        Args:
            path: Local path of the blueprint.
            info: Newly scanned blueprint info.
            prev: Previous metadata dictionary.
            prev_url: Explicit previous source URL to compare against.
                Defaults to prev["source_url"].

        Returns:
            Updated (possibly invalidated) metadata dictionary.

        """
        resolved_prev_url = (
            prev_url
            if isinstance(prev_url, str)
            else (prev.get("source_url") if isinstance(prev, dict) else None)
        )
        curr_url = info.get("source_url")

        if (
            isinstance(resolved_prev_url, str)
            and isinstance(curr_url, str)
            and resolved_prev_url != curr_url
        ):
            return self._invalidate_blueprint_metadata(
                path, resolved_prev_url, curr_url, prev if isinstance(prev, dict) else info
            )
        return prev if isinstance(prev, dict) else info

    def _invalidate_blueprint_metadata(
        self, path: str, prev_url: str, curr_url: str, prev: dict[str, object]
    ) -> dict[str, object]:
        """Invalidate all remote-derived metadata for a blueprint.

        This is called when the source URL changes, ensuring that old ETags,
        hashes, and content are cleared from both memory and disk.

        Args:
            path: Local path of the blueprint.
            prev_url: The previous source URL.
            curr_url: The new source URL.
            prev: The current metadata dictionary to be invalidated.

        Returns:
            The invalidated metadata dictionary.

        """
        _LOGGER.info(
            "Source URL changed for %s (%s -> %s); clearing remote cache",
            path,
            redact_url(prev_url),
            redact_url(curr_url),
        )
        if not (relative_path := prev.get("relative_path")):
            relative_path = get_blueprint_relative_path(self.hass, path)

        if relative_path and isinstance(relative_path, str):
            self._persisted_metadata.pop(relative_path, None)

        invalidated = {
            **prev,
            "remote_hash": None,
            "invalid_remote_hash": None,
            "remote_content": None,
            "last_error": None,
            "etag": None,
            "last_modified": None,
            "updatable": False,
        }
        if self.data is not None:
            self.data[path] = invalidated

        self.hass.async_create_background_task(
            self._async_save_metadata(force=True), name=f"{DOMAIN}_url_change_save"
        )

        return invalidated

    def _apply_ghost_update_detection(
        self, path: str, info: dict[str, object], prev_data: dict[str, object]
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
            return False, None, None, str(local_hash) if local_hash else None

        return (
            is_updatable,
            str(next_invalid) if isinstance(next_invalid, str) else None,
            str(next_error) if isinstance(next_error, str) else None,
            str(remote_hash) if isinstance(remote_hash, str) else None,
        )

    def _is_ghost_update(self, current_local_hash: object, prev_data: dict[str, object]) -> bool:
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
        remote_content = prev_data.get("remote_content")
        source_url = prev_data.get("source_url")
        if (
            not isinstance(remote_content, str)
            or not isinstance(current_local_hash, str)
            or not isinstance(source_url, str)
        ):
            return False
        return self._is_semantically_equal(remote_content, current_local_hash, source_url)

    def _start_background_refresh(
        self,
        blueprints: Mapping[str, Mapping[str, object]],
        generation: int | None = None,
    ) -> None:
        """Start background work for the newest local scan generation.

        Args:
            blueprints: Dictionary of blueprints to scan remotely.
            generation: Identity of the local scan that produced the snapshot.

        """
        if generation is None:
            generation = self._refresh_generation
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Cancelling obsolete background refresh generation")
            self._background_task.cancel()

        self._background_task = self.hass.async_create_background_task(
            self._async_background_refresh(blueprints, generation),
            name=f"{DOMAIN}_background_refresh",
        )

    @callback
    def _async_cancel_background_task(self) -> None:
        """Cancel the background task on unload."""
        self._refresh_generation += 1
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Cancelling background refresh task on unload")
            self._background_task.cancel()

    def _is_current_refresh_item(
        self,
        work_item: RefreshWorkItem | None,
    ) -> bool:
        """Return whether work is direct or still owned by the authoritative scan."""
        return work_item is None or work_item.matches(self._refresh_generation, self.data)

    async def _async_background_refresh(
        self,
        blueprints: Mapping[str, Mapping[str, object]],
        generation: int | None = None,
    ) -> None:
        """Fetch remote updates in the background using a task queue.

        This method initializes a pool of background workers to process
        blueprint updates concurrently. It ensures that workers are cleaned up
        gracefully by enqueuing a sentinel (None) for each worker and waiting
        for them to terminate using asyncio.gather, even if the task is canceled
        while awaiting the queue to join.

        Args:
            blueprints: Dictionary of blueprints to check for updates.
            generation: Identity of the authoritative local scan, when owned.

        """
        session = None
        try:
            if generation is not None and generation != self._refresh_generation:
                return

            async with self._refresh_lock:
                if generation is not None and generation != self._refresh_generation:
                    return
                await self._async_retry_pending_reloads()
                if generation is not None and generation != self._refresh_generation:
                    return
                async with self._safe_hostname_lock:
                    self._safe_hostname_cache.clear()
                results_to_notify: list[str] = []
                updated_domains: set[str] = set()
                queue: asyncio.Queue[
                    tuple[str, dict[str, object], RefreshWorkItem | None] | None
                ] = asyncio.Queue()

                for path, info in blueprints.items():
                    info_dict = dict(info)
                    work_item = (
                        RefreshWorkItem.capture(generation, path, info_dict)
                        if generation is not None
                        else None
                    )
                    queue.put_nowait((path, info_dict, work_item))

                session = get_guarded_async_client(self.hass, **self._get_client_kwargs())

                async def _worker() -> None:
                    """Process blueprints from the queue."""
                    while True:
                        item = await queue.get()
                        if item is None:
                            queue.task_done()
                            break

                        blueprint_path, blueprint_info, work_item = item
                        try:
                            if not self._is_current_refresh_item(work_item):
                                continue
                            await self._async_update_blueprint_in_place(
                                session,
                                blueprint_path,
                                blueprint_info,
                                results_to_notify,
                                updated_domains,
                                refresh_work=work_item,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _LOGGER.exception("Error in background worker for %s", blueprint_path)
                        finally:
                            queue.task_done()

                workers = [
                    self.hass.async_create_background_task(_worker(), name=f"{DOMAIN}_worker_{i}")
                    for i in range(MAX_CONCURRENT_REQUESTS)
                ]

                cancellation: asyncio.CancelledError | None = None
                try:
                    if workers:
                        await queue.join()
                except asyncio.CancelledError as err:
                    cancellation = err
                    for worker in workers:
                        worker.cancel()
                finally:
                    if cancellation is None:
                        for _ in workers:
                            await queue.put(None)
                    if workers:
                        await asyncio.gather(*workers, return_exceptions=True)

                if cancellation is not None:
                    if results_to_notify:
                        await asyncio.shield(self.async_reconcile_reload_services(updated_domains))
                    raise cancellation

                if not queue.empty():
                    _LOGGER.warning(
                        "Background refresh finished with %d unprocessed items in queue",
                        queue.qsize(),
                    )

                _LOGGER.debug("Background refresh complete")
                if results_to_notify:
                    await self._async_handle_notifications(results_to_notify, updated_domains)
                elif generation is None or generation == self._refresh_generation:
                    await self._async_save_metadata()
                if generation is None or generation == self._refresh_generation:
                    self.async_set_updated_data(self.data)
        finally:
            if asyncio.current_task() is self._background_task:
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

        final_metadata: dict[str, dict[str, object]] = {}
        all_relative_paths = set(self._persisted_metadata.keys())
        for _, info in self.data.items():
            if (relative_path := info.get("relative_path")) and isinstance(relative_path, str):
                all_relative_paths.add(relative_path)

        blueprints_root = self.hass.config.path(BLUEPRINTS_DATA_DIR)
        current_data_map = {
            i["relative_path"]: i for i in self.data.values() if i.get("relative_path")
        }
        candidate_metadata = {}
        for relative_path in all_relative_paths:
            existing = dict(self._persisted_metadata.get(relative_path, {}))
            if info := current_data_map.get(relative_path):
                for field in ("remote_hash", "etag", "last_modified", "source_url"):
                    existing[field] = info.get(field)
            candidate_metadata[relative_path] = existing

        if not skip_filter:
            valid_metadata = await self.hass.async_add_executor_job(
                self._filter_existing_metadata, blueprints_root, candidate_metadata
            )
            final_metadata = {
                k: v for k, v in valid_metadata.items() if self._has_meaningful_metadata(v)
            }
        else:
            final_metadata = {
                k: v for k, v in candidate_metadata.items() if self._has_meaningful_metadata(v)
            }

        if (
            not force
            and final_metadata == self._persisted_metadata
            and self._pending_reload_domains == self._persisted_pending_reload_domains
        ):
            return

        _LOGGER.debug(
            "Saving metadata for %d blueprints to storage",
            len(final_metadata),
        )
        try:
            await self._store.async_save(
                {
                    "metadata": final_metadata,
                    "pending_reload_domains": sorted(self._pending_reload_domains),
                }
            )
            self._persisted_metadata = final_metadata
            self._persisted_pending_reload_domains = set(self._pending_reload_domains)
        except Exception:
            _LOGGER.exception("Failed to save metadata to storage")

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and cancel tasks."""
        self._refresh_generation += 1
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Cancelling background refresh task due to shutdown")
            self._background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_task
            self._background_task = None

    def _mark_pending_reload_state(self) -> None:
        """Expose pending reload state on affected coordinator entries."""
        for info in self.data.values():
            info["reload_pending"] = info.get("domain") in self._pending_reload_domains

    async def async_reconcile_reload_services(
        self,
        domains: Iterable[str | FunctionalDomain] | None = None,
    ) -> set[str]:
        """Reload domains while durably retaining failures for later retry."""
        async with self._reload_lock:
            targets = (
                set(ALLOWED_RELOAD_DOMAINS)
                if domains is None
                else {domain for domain in domains if domain in ALLOWED_RELOAD_DOMAINS}
            )
            if not targets:
                return set()

            self._pending_reload_domains.update(targets)
            self._mark_pending_reload_state()
            await self._async_save_metadata(force=True)

            unreloaded: set[str] = set()
            for domain in sorted(targets):
                try:
                    reloaded = await self.async_reload_services([domain])
                except Exception:
                    unreloaded.add(domain)
                    _LOGGER.exception(
                        "Blueprint files were committed, but %s.reload failed; "
                        "the reload remains pending",
                        domain,
                    )
                else:
                    if domain in reloaded:
                        self._pending_reload_domains.discard(domain)
                    else:
                        unreloaded.add(domain)
                        _LOGGER.debug(
                            "%s.reload is unavailable; the reload remains pending",
                            domain,
                        )

            self._mark_pending_reload_state()
            await self._async_save_metadata(force=True)
            return unreloaded

    async def _async_retry_pending_reloads(self) -> None:
        """Retry reloads left pending by an earlier committed operation."""
        if self._pending_reload_domains:
            await self.async_reconcile_reload_services(set(self._pending_reload_domains))

    @staticmethod
    def _has_meaningful_metadata(entry: dict[str, object]) -> bool:
        """Return True if this metadata entry has any meaningful value set.

        Uses ``is not None`` checks rather than truthiness so that falsy-but-
        meaningful values (empty strings, 0, False) are not discarded.
        """
        return any(entry.get(field) is not None for field in METADATA_STORAGE_FIELDS)

    @staticmethod
    def _get_client_kwargs() -> dict[str, tuple[str, ...] | None]:
        """Get the default httpx client kwargs with ALPN support if available."""
        if BlueprintUpdateCoordinator._client_kwargs_cache is not None:
            return BlueprintUpdateCoordinator._client_kwargs_cache

        client_kwargs: dict[str, tuple[str, ...] | None] = (
            {"alpn_protocols": SSL_ALPN_HTTP11_HTTP2} if SSL_ALPN_HTTP11_HTTP2 is not None else {}
        )

        BlueprintUpdateCoordinator._client_kwargs_cache = client_kwargs
        return client_kwargs

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
        await self.async_reconcile_reload_services(domains)

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
        except Exception:
            _LOGGER.exception("Failed to send auto-update notification")

    @staticmethod
    def _extract_defined_inputs(input_dict: object) -> set[str]:
        """Extract all input keys defined in blueprint.input (including nested sections).

        Args:
            input_dict: The raw input mapping from the blueprint block.

        Returns:
            A set of all defined input key names.

        """
        keys: set[str] = set()
        if not isinstance(input_dict, Mapping):
            return keys

        for k, v in input_dict.items():
            if isinstance(v, Mapping) and "input" in v and isinstance(v["input"], Mapping):
                keys.update(BlueprintUpdateCoordinator._extract_defined_inputs(v["input"]))
            elif isinstance(k, str):
                keys.add(k)
        return keys

    @staticmethod
    def _extract_used_inputs(obj: object) -> list[str]:
        """Recursively find all !input references in the parsed structure.

        Args:
            obj: Parsed blueprint YAML data or nested object.

        Returns:
            A list of input names referenced via !input tags.

        """
        used: list[str] = []
        if isinstance(obj, Input):
            used.append(obj.name)
        elif isinstance(obj, Mapping):
            for k, v in obj.items():
                if isinstance(k, Input):
                    used.append(k.name)
                elif not isinstance(k, (str, bytes, bytearray)):
                    used.extend(BlueprintUpdateCoordinator._extract_used_inputs(k))
                used.extend(BlueprintUpdateCoordinator._extract_used_inputs(v))
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            for item in obj:
                used.extend(BlueprintUpdateCoordinator._extract_used_inputs(item))
        return used

    @staticmethod
    def _validate_input_references(data: dict[str, object]) -> str | None:
        """Verify that all !input tags reference defined blueprint inputs.

        Args:
            data: Parsed YAML dictionary of the blueprint.

        Returns:
            An error message if undefined inputs are referenced, or None if valid.

        """
        blueprint_meta = data.get("blueprint")
        if not isinstance(blueprint_meta, Mapping):
            return None

        defined = BlueprintUpdateCoordinator._extract_defined_inputs(blueprint_meta.get("input"))
        used = BlueprintUpdateCoordinator._extract_used_inputs(data)

        if undefined := sorted({name for name in used if name not in defined}):
            if len(undefined) == 1:
                return f"Undefined input referenced: '!input {undefined[0]}'"
            formatted = ", ".join(f"'!input {name}'" for name in undefined)
            return f"Undefined inputs referenced: {formatted}"

        return None

    def _validate_blueprint(
        self,
        data: dict[str, object],
        source_url: str,
        expected_domain: str,
    ) -> str | None:
        """Validate blueprint data using HA Core's Blueprint class.

        Performs basic structure check, structural validation,
        min_version compatibility check, and Jinja2 syntax validation.

        Args:
            data: Parsed YAML dictionary of the blueprint.
            source_url: The URL the blueprint was loaded from (for logging).
            expected_domain: The expected domain (automation/script/template) based on folder.

        Returns:
            An error string key if validation fails, or None if valid.

        """
        if (
            not isinstance(data, dict)
            or "blueprint" not in data
            or not isinstance(data["blueprint"], dict)
        ):
            _LOGGER.warning(
                "Remote content from %s is not a valid blueprint (missing 'blueprint' key)",
                redact_url(source_url),
            )
            return "invalid_blueprint"

        if input_error := BlueprintUpdateCoordinator._validate_input_references(data):
            _LOGGER.warning(
                "Blueprint input validation failed for %s: %s",
                redact_url(source_url),
                input_error,
            )
            return format_error_message("blueprint_validation_error", input_error)

        schema = BlueprintUpdateCoordinator._get_blueprint_schema(expected_domain)

        try:
            bp = Blueprint(data, expected_domain=expected_domain, schema=schema)
            if errors := bp.validate():
                error_msg = "; ".join(errors)
                _LOGGER.warning(
                    "Blueprint from %s is incompatible: %s",
                    redact_url(source_url),
                    error_msg,
                )
                return format_error_message("incompatible", error_msg)
        except InvalidBlueprint as err:
            _LOGGER.warning(
                "Blueprint validation failed for %s: %s",
                redact_url(source_url),
                err,
            )
            return format_error_message("blueprint_validation_error", err)

        if template_error := self._validate_template_value(data, "", skip_blueprint_metadata=True):
            path, error = template_error
            error_msg = sanitize_error_detail(f"Invalid template at {path}: {error}")
            _LOGGER.warning(
                "Blueprint template validation failed for %s: %s",
                redact_url(source_url),
                error_msg,
            )
            return format_error_message("blueprint_validation_error", error_msg)

        return None

    def _validate_template_value(
        self,
        value: object,
        path: str,
        *,
        skip_blueprint_metadata: bool = False,
    ) -> tuple[str, str] | None:
        """Validate one value and recursively inspect nested YAML structures."""
        if isinstance(value, str):
            if not is_template_string(value):
                return None
            try:
                Template(value, self.hass).ensure_valid()
            except TemplateError as err:
                return path, str(err)
            return None

        if isinstance(value, Mapping):
            for key, child in value.items():
                if skip_blueprint_metadata and key == "blueprint":
                    continue
                child_path = self._template_path(path, key)
                if isinstance(key, str) and (
                    template_error := self._validate_template_value(key, child_path)
                ):
                    return template_error
                if template_error := self._validate_template_value(child, child_path):
                    return template_error
            return None

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, child in enumerate(value):
                if template_error := self._validate_template_value(child, f"{path}[{index}]"):
                    return template_error

        return None

    @staticmethod
    def _template_path(path: str, key: object) -> str:
        """Build a concrete YAML-style path for a mapping entry."""
        if not path:
            return str(key)
        if isinstance(key, str) and key.isidentifier():
            return f"{path}.{key}"
        return f"{path}[{key!r}]"

    def _get_functional_domain(
        self,
        path: str,
        content: str | None = None,
        parsed_data: dict[str, object] | None = None,
    ) -> str:
        """Determine the functional domain for a blueprint path.

        Priority:
        1. Cached domain in self.data (normalized and validated).
        2. Directory structure parsing from path.
        3. Metadata parsing from content or pre-parsed data.
        4. Default to 'automation'.

        Args:
            path: Local path to the blueprint.
            content: Raw YAML content.
            parsed_data: Pre-parsed YAML content dictionary.

        Returns:
            The determined domain string.

        """
        if self.data and (cached_info := self.data.get(path)):
            cached_domain = cached_info.get("domain")
            if isinstance(cached_domain, str):
                normalized_domain = cached_domain.strip().lower()
                if normalized_domain in ALLOWED_RELOAD_DOMAINS:
                    return normalized_domain

        if relative_path := get_blueprint_relative_path(self.hass, path):
            domain = relative_path.split("/", 1)[0]
            if domain in ALLOWED_RELOAD_DOMAINS:
                return domain

        bp_block = self._get_blueprint_block(path, content, parsed_data=parsed_data)
        if bp_block:
            return normalize_domain(bp_block.get("domain"))

        return FunctionalDomain.AUTOMATION

    async def async_reload_services(
        self,
        domains: list[str] | set[str] | None = None,
    ) -> set[str]:
        """Reload specific domains or default ones if they are allowed.

        Allowed domains are limited to automation, script, and template
        to prevent malicious blueprints from triggering unintended reloads.

        Args:
            domains: List of domains to reload. If None, reloads all allowed.

        Returns:
            Domains whose registered reload service completed successfully.

        """
        if domains:
            targets = [d for d in domains if d in ALLOWED_RELOAD_DOMAINS]
        else:
            targets = list(ALLOWED_RELOAD_DOMAINS)

        reloaded: set[str] = set()
        for domain in targets:
            if self.hass.services.has_service(domain, "reload"):
                await self.hass.services.async_call(domain, "reload")
                reloaded.add(domain)
        return reloaded

    async def async_fetch_import_data(self, url: str) -> tuple[str, str, str, str, httpx.Response]:
        """Fetch blueprint content, canonical url, and validate basic metadata."""
        return await self._async_fetch_import_data(url)

    async def async_detect_risks_for_update(
        self,
        path: str,
        info: Mapping[str, object],
        remote_content: str,
        session: httpx.AsyncClient | None = None,
    ) -> list[StructuredRisk]:
        """Detect potential breaking changes for a blueprint update."""
        return await self._detect_risks_for_update(path, info, remote_content, session=session)

    async def _async_fetch_import_data(self, url: str) -> tuple[str, str, str, str, httpx.Response]:
        """Fetch blueprint content, canonical url, and validate basic metadata."""
        if not await self._is_safe_url(url):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsafe_blueprint_url",
                translation_placeholders={"url": redact_url(url)},
            )

        provider = registry.get_provider(url)
        if not provider:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_source",
            )

        canonical_url = provider.normalize_url(url)
        if not await self._is_safe_url(canonical_url):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsafe_blueprint_url",
                translation_placeholders={"url": redact_url(canonical_url)},
            )

        session = get_guarded_async_client(self.hass, **self._get_client_kwargs())
        try:
            response = await self._execute_with_redirect_guard(session, canonical_url, {})

            if provider.provider_type == SourceProviderType.GENERIC:
                content_type_raw = response.headers.get("Content-Type", "").lower()
                media_type = content_type_raw.split(";")[0].strip()
                allowed = ALLOWED_YAML_MIME_TYPES
                if media_type not in allowed:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="invalid_content_type",
                        translation_placeholders={"content_type": content_type_raw},
                    )

            content = await self._parse_provider_response(response, canonical_url)
            if not content:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="empty_blueprint_content",
                )
        except (httpx.HTTPError, HomeAssistantError) as err:
            if isinstance(err, ServiceValidationError):
                raise
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="fetch_blueprint_error",
                translation_placeholders={"error": sanitize_error_detail(str(err))},
            ) from err

        try:
            metadata = provider.get_metadata(
                canonical_url,
                content=self._decode_response_text(response, canonical_url),
            )
            author = metadata["author"]
            name = metadata["name"]
        except (KeyError, TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="fetch_blueprint_error",
                translation_placeholders={"error": f"Malformed metadata: {err}"},
            ) from err

        return content, canonical_url, author, name, response

    @staticmethod
    def _read_import_source_url(full_path: str) -> object:
        """Read an existing import target's source URL off the event loop."""
        try:
            with open(full_path, encoding="utf-8") as file:
                content_on_disk = file.read()
            parsed_disk = yaml_util.parse_yaml(content_on_disk)
            if isinstance(parsed_disk, dict):
                blueprint_section = parsed_disk.get("blueprint")
                if isinstance(blueprint_section, dict):
                    return blueprint_section.get("source_url")
        except (OSError, UnicodeDecodeError, HomeAssistantError) as err:
            _LOGGER.debug(
                "Failed to read existing blueprint file %s to determine source_url: %s",
                full_path,
                err,
            )
        return None

    async def _check_import_path_conflicts(
        self, full_path: str, rel_path: str, canonical_url: str
    ) -> FileRevisionPrecondition:
        """Check import conflicts and capture the target revision for commit."""
        if not self._is_safe_path(full_path):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsafe_blueprint_path",
                translation_placeholders={"path": rel_path},
            )

        try:
            precondition = await self.hass.async_add_executor_job(
                BlueprintFileStore.capture_precondition,
                full_path,
            )
        except FileRevisionMismatchError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="import_path_conflict",
                translation_placeholders={"existing_url": rel_path},
            ) from err

        existing_url = None
        if full_path in self.data:
            existing_url = self.data[full_path].get("source_url")
        elif precondition.must_exist:
            existing_url = await self.hass.async_add_executor_job(
                self._read_import_source_url,
                full_path,
            )

        if existing_url:
            if not isinstance(existing_url, str):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="import_invalid_source_type",
                    translation_placeholders={"type": type(existing_url).__name__},
                )
            norm_existing = registry.normalize_url(existing_url)
            if norm_existing != canonical_url:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="import_path_conflict",
                    translation_placeholders={"existing_url": redact_url(existing_url)},
                )
        elif precondition.must_exist:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="import_path_conflict",
                translation_placeholders={"existing_url": rel_path},
            )
        return precondition

    async def async_import_blueprint(self, url: str, confirm: bool = False) -> None:
        """Import a new blueprint from a URL.

        This method fetches, validates, and installs a blueprint. It uses raw components
        (author/name) for the destination path instead of slugifying them. This is a
        deliberate design choice to maintain 100% parity with Home Assistant Core's
        internal blueprint importer (see homeassistant/components/blueprint/importer.py).

        By avoiding slugification, we ensure that:
        1. Hostnames (e.g., 'pastebin.com') or platform usernames remain as-is in the
           folder structure, matching exactly how HA Core stores them.
        2. Blueprints imported via this service will resolve to the exact same file
           system path as those imported via the Home Assistant UI, preventing
           duplicate entries and ensuring seamless update management.
        """
        if not confirm:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="confirmation_required",
            )

        content, canonical_url, author, name, response = await self._async_fetch_import_data(url)

        try:
            parsed = yaml_util.parse_yaml(content)
            if not isinstance(parsed, dict) or "blueprint" not in parsed:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_yaml",
                )
            functional_domain = self._get_functional_domain(
                "imported.yaml", content=content, parsed_data=parsed
            )
            domain = functional_domain
        except HomeAssistantError as err:
            if isinstance(err, ServiceValidationError):
                raise
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_yaml",
            ) from err

        rel_path = f"{domain}/{author}/{name}.yaml"
        full_path = self.hass.config.path(BLUEPRINTS_DATA_DIR, rel_path)

        file_precondition = await self._check_import_path_conflicts(
            full_path,
            rel_path,
            canonical_url,
        )

        if validation_error := self._validate_blueprint(parsed, canonical_url, domain):
            error_parts = split_error_message(validation_error)
            error_key, error_detail = error_parts or (validation_error, validation_error)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=error_key,
                translation_placeholders={"error": error_detail},
            )

        await self.async_install_blueprint(
            full_path,
            content,
            source_url=canonical_url,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            file_precondition=file_precondition,
        )

        await self.async_request_refresh()

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

        session = get_guarded_async_client(self.hass, **self._get_client_kwargs())
        results_to_notify: list[str] = []
        updated_domains: set[str] = set()

        await self._async_update_blueprint_in_place(
            session, path, info, results_to_notify, updated_domains, force=force
        )
        self.async_set_updated_data(self.data)

    @staticmethod
    def _get_backup_path(file_path: str, version: int | str) -> str:
        """Construct the path to a specific backup file version."""
        return BlueprintFileStore.backup_path(file_path, version)

    @staticmethod
    def _count_backups_sync(file_path: str, max_bak: int) -> int:
        """Count the number of existing backup files for a given blueprint path."""
        return BlueprintFileStore.count_backups(file_path, max_bak)

    @staticmethod
    def _check_backup_exists_sync(file_path: str, version: int) -> bool:
        """Check if a specific backup file exists."""
        bak_path = BlueprintUpdateCoordinator._get_backup_path(file_path, version)
        return os.path.isfile(bak_path)

    async def async_check_backup_exists(self, path: str, version: int) -> bool:
        """Check if a specific backup version exists on disk.

        Runs the check in the executor to avoid blocking the event loop.
        """
        max_backups = get_max_backups(self.config_entry)
        if version < 1 or version > max_backups:
            return False
        real_path = os.path.realpath(path)
        if not self._is_safe_path(real_path):
            return False
        return await self.hass.async_add_executor_job(
            BlueprintUpdateCoordinator._check_backup_exists_sync, real_path, version
        )

    @staticmethod
    def _rotate_backups(file_path: str, max_bak: int) -> None:
        """Create a verified backup and rotate numbered backup files.

        Args:
            file_path: Path to the active file to rotate.
            max_bak: Maximum number of backups to keep.

        """
        BlueprintFileStore.rotate_backups(file_path, max_bak)

    async def async_install_blueprint(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = True,
        remote_hash: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        is_auto_update: bool = False,
        source_url: str | None = None,
        file_precondition: FileRevisionPrecondition | None = None,
        refresh_work: RefreshWorkItem | None = None,
    ) -> None:
        """Serialize and install a blueprint as one per-path transaction."""
        current = self.data.get(path)
        if file_precondition is None and current:
            current_file_hash = current.get("local_file_hash")
            if isinstance(current_file_hash, str):
                file_precondition = FileRevisionPrecondition.existing(current_file_hash)
        real_path = os.path.realpath(path)
        async with self._file_store.transaction(real_path):
            if not self._is_current_refresh_item(refresh_work):
                raise BlueprintRefreshObsoleteError(
                    "Blueprint refresh became obsolete before installation"
                )
            await self._async_install_blueprint_locked(
                path,
                remote_content,
                reload_services=reload_services,
                backup=backup,
                remote_hash=remote_hash,
                etag=etag,
                last_modified=last_modified,
                is_auto_update=is_auto_update,
                source_url=source_url,
                file_precondition=file_precondition,
            )

    async def _async_install_blueprint_locked(
        self,
        path: str,
        remote_content: str,
        reload_services: bool = True,
        backup: bool = True,
        remote_hash: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        is_auto_update: bool = False,
        source_url: str | None = None,
        file_precondition: FileRevisionPrecondition | None = None,
    ) -> None:
        """Install a blueprint while its canonical path lock is held.

        This method validates the blueprint path, creates a backup if requested,
        writes the new content, and optionally reloads the associated services.
        It also performs domain validation, logging a warning if the declared
        domain in the YAML does not match the functional domain derived from
        the directory structure. If YAML parsing fails during this validation,
        a warning is logged but the installation continues using the functional
        domain.

        Fires EVENT_BLUEPRINTS_UPDATER_UPDATED upon success.

        Args:
            path: Target filesystem path for the blueprint.
            remote_content: Raw YAML content to write.
            reload_services: Whether to reload HA services after writing.
            backup: Whether to create backup files of the old version.
            remote_hash: Optional pre-computed hash of the remote content.
            etag: Optional ETag associated with the remote content.
            last_modified: Optional Last-Modified associated with the remote content.
            is_auto_update: Whether this is an automatic update.
            source_url: Optional source URL for event reporting.
            file_precondition: Expected target revision at commit time.

        Raises:
            HomeAssistantError: If the path is unsafe or content is empty.

        """
        try:
            prepared = self._prepare_blueprint_install(
                path,
                remote_content,
                remote_hash=remote_hash,
                source_url=source_url,
            )
            file_result = await self.hass.async_add_executor_job(
                self._file_store.install,
                prepared.real_path,
                prepared.content,
                get_max_backups(self.config_entry),
                backup,
                file_precondition,
            )
            await self._async_finalize_blueprint_install(
                path,
                prepared,
                file_result,
                reload_services=reload_services,
                etag=etag,
                last_modified=last_modified,
                is_auto_update=is_auto_update,
            )
        except FileRevisionMismatchError as err:
            _LOGGER.warning("Rejected stale blueprint update at %s: %s", path, err)
            raise HomeAssistantError(_LOCAL_REVISION_MISMATCH_ERROR) from err
        except Exception:
            _LOGGER.exception("Failed to update blueprint at %s", path)
            raise

    def _prepare_blueprint_install(
        self,
        path: str,
        remote_content: str,
        remote_hash: str | None,
        source_url: str | None,
    ) -> PreparedBlueprintInstall:
        """Validate and normalize one blueprint installation."""
        real_path = os.path.realpath(path)
        self._validate_blueprint_install_request(path, real_path, remote_content)
        parsed = self._parse_blueprint_install_content(path, remote_content)
        functional_domain = self._get_functional_domain(
            path,
            content=remote_content if parsed else None,
            parsed_data=parsed,
        )
        blueprint_block = self._get_blueprint_block(path, parsed_data=parsed) if parsed else None
        metadata = self._resolve_blueprint_metadata(
            path,
            blueprint_block,
            real_path,
            source_url=source_url,
        )
        final_source_url_val = metadata.get("source_url")
        final_source_url = (
            str(final_source_url_val) if isinstance(final_source_url_val, str) else None
        )
        content = (
            self._ensure_source_url(remote_content, final_source_url)
            if final_source_url
            else self._normalize_content(remote_content)
        )
        expected_hash = self._hash_content(content, already_normalized=True)
        if remote_hash is not None and remote_hash != expected_hash:
            raise HomeAssistantError("Remote hash does not match validated install content")

        current_val = metadata.get("current")
        current_dict: dict[str, object] | None = (
            {str(k): v for k, v in current_val.items()} if isinstance(current_val, dict) else None
        )
        name_val = metadata.get("name")
        name_str = str(name_val) if name_val is not None else ""
        rel_val = metadata.get("relative_path")
        rel_str = str(rel_val) if rel_val is not None else ""

        return PreparedBlueprintInstall(
            real_path=real_path,
            parsed=parsed,
            blueprint_block=blueprint_block,
            functional_domain=functional_domain,
            current=current_dict,
            name=name_str,
            source_url=final_source_url,
            relative_path=rel_str,
            content=content,
        )

    def _validate_blueprint_install_request(
        self,
        path: str,
        real_path: str,
        remote_content: str,
    ) -> None:
        """Reject unsafe paths and empty blueprint content."""
        if not self._is_safe_path(real_path):
            _LOGGER.error("Security violation: Attempted to install to unsafe path: %s", real_path)
            raise HomeAssistantError(
                "Security violation: Attempted to install to an unsafe location"
            )
        if not remote_content:
            _LOGGER.error("Cannot install blueprint at %s: content is empty or None", path)
            raise HomeAssistantError("Blueprint content is missing or empty")

    @staticmethod
    def _parse_blueprint_install_content(
        path: str,
        remote_content: str,
    ) -> dict[str, object] | None:
        """Parse install content, retaining the existing permissive fallback."""
        try:
            parsed = yaml_util.parse_yaml(remote_content)
        except HomeAssistantError as err:
            _LOGGER.warning("Failed to parse blueprint at %s", path)
            _LOGGER.debug("Blueprint YAML parse error at %s: %s", path, err, exc_info=err)
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _warn_if_blueprint_domain_mismatches(
        path: str,
        parsed: dict[str, object] | None,
        functional_domain: str,
    ) -> None:
        """Warn when YAML metadata conflicts with the path-derived domain."""
        blueprint_meta = parsed.get("blueprint") if parsed else None
        declared_domain_raw = (
            blueprint_meta.get("domain") if isinstance(blueprint_meta, dict) else None
        )
        declared_domain = (
            declared_domain_raw.strip().lower() if isinstance(declared_domain_raw, str) else None
        )
        if declared_domain and declared_domain != functional_domain:
            _LOGGER.warning(
                "Blueprint at %s has declared domain '%s' that does "
                "not match functional domain '%s'; falling back to "
                "functional domain",
                path,
                declared_domain,
                functional_domain,
            )

    @staticmethod
    def _build_installed_blueprint_metadata(
        prepared: PreparedBlueprintInstall,
        file_result: FileTransactionResult,
        etag: str | None,
        last_modified: str | None,
    ) -> dict[str, object]:
        """Build the synchronized coordinator state for an installation."""
        current = prepared.current
        final_etag = etag if etag is not None else (current.get("etag") if current else None)
        final_last_modified = (
            last_modified
            if last_modified is not None
            else (current.get("last_modified") if current else None)
        )
        return {
            "name": prepared.name,
            "domain": prepared.functional_domain,
            "source_url": prepared.source_url,
            "relative_path": prepared.relative_path,
            "updatable": False,
            "local_hash": file_result.content_hash,
            "local_file_hash": file_result.content_hash,
            "remote_hash": file_result.content_hash,
            "last_error": None,
            "auto_update_last_error": None,
            "remote_content": None,
            "invalid_remote_hash": None,
            "breaking_risks": [],
            "update_blocking_reason": None,
            "etag": final_etag,
            "last_modified": final_last_modified,
            "backups_count": file_result.backups_count,
            "_cached_git_diff": None,
        }

    async def _async_store_installed_blueprint_metadata(
        self,
        path: str,
        blueprint_block: dict[str, object] | None,
        metadata: dict[str, object],
        persist: bool = True,
    ) -> None:
        """Persist install metadata when the blueprint is tracked or parseable."""
        if path in self.data:
            self.data[path].update(metadata)
        elif blueprint_block:
            self.data[path] = metadata
        else:
            return
        if persist:
            await self._async_save_metadata(force=True)
        self.async_set_updated_data(self.data)

    async def _async_finalize_blueprint_install(
        self,
        path: str,
        prepared: PreparedBlueprintInstall,
        file_result: FileTransactionResult,
        reload_services: bool,
        etag: str | None,
        last_modified: str | None,
        is_auto_update: bool,
    ) -> None:
        """Reconcile committed state, announce it, and recover reload failures."""
        self._warn_if_blueprint_domain_mismatches(
            path,
            prepared.parsed,
            prepared.functional_domain,
        )
        current = prepared.current
        prev_hash_val = current.get("local_hash") if current else None
        previous_hash = str(prev_hash_val) if prev_hash_val is not None else None
        had_breaking_risks = bool(current.get("breaking_risks")) if current else False
        metadata = self._build_installed_blueprint_metadata(
            prepared,
            file_result,
            etag,
            last_modified,
        )
        await self._async_store_installed_blueprint_metadata(
            path,
            prepared.blueprint_block,
            metadata,
            persist=not reload_services,
        )
        self._fire_update_event(
            blueprint_name=prepared.name,
            domain=prepared.functional_domain,
            relative_path=prepared.relative_path,
            source_url=prepared.source_url,
            previous_hash=previous_hash,
            new_hash=file_result.content_hash,
            is_auto_update=is_auto_update,
            had_breaking_risks=had_breaking_risks,
        )
        if reload_services:
            await self.async_reconcile_reload_services([prepared.functional_domain])
        _LOGGER.info("Blueprint at %s updated successfully", prepared.real_path)

    def _resolve_blueprint_metadata(
        self,
        path: str,
        bp_block: dict[str, object] | None,
        real_path: str,
        source_url: str | None = None,
    ) -> dict[str, object]:
        """Resolve blueprint metadata merging new content with cached data.

        This method merges newly parsed content with existing cache. It:
        1. Resolves the blueprint name, preferring the new content.
        2. Implements Strict URL Persistence: the cached source_url is always
           preserved if it exists to maintain update tracking stability and
           prevent URL hijacking from blueprint content.
        3. Resolves the relative path based on the actual file location.
        """
        current = self.data.get(path) if self.data else None

        name = (
            (bp_block.get("name") if bp_block else None)
            or (current.get("name") if current else None)
            or "Unknown"
        )

        raw_source_url = (
            (current.get("source_url") if current else None)
            or source_url
            or (bp_block.get("source_url") if bp_block else None)
        )

        if isinstance(raw_source_url, str):
            final_source_url = raw_source_url
        elif raw_source_url is None:
            final_source_url = None
        else:
            _LOGGER.warning(
                "Blueprint metadata for %s contains non-string source_url (%r); ignoring it",
                path,
                raw_source_url,
            )
            final_source_url = None

        relative_path = get_blueprint_relative_path(self.hass, real_path) or (
            str(current.get("relative_path")) if (current and current.get("relative_path")) else ""
        )

        current_dict: dict[str, object] | None = (
            {str(k): v for k, v in current.items()} if isinstance(current, Mapping) else None
        )

        return {
            "name": str(name),
            "source_url": final_source_url,
            "relative_path": relative_path,
            "current": current_dict,
        }

    def _fire_update_event(
        self,
        blueprint_name: str,
        domain: str,
        relative_path: str,
        new_hash: str,
        is_auto_update: bool,
        had_breaking_risks: bool,
        source_url: str | None = None,
        previous_hash: str | None = None,
    ) -> None:
        """Fire a standardized update event for external consumers."""
        payload: BlueprintUpdateEventPayload = {
            "blueprint_name": blueprint_name,
            "domain": domain,
            "relative_path": relative_path,
            "source_url": source_url,
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "is_auto_update": is_auto_update,
            "had_breaking_risks": had_breaking_risks,
        }
        self.hass.bus.async_fire(EVENT_BLUEPRINTS_UPDATER_UPDATED, payload)

    async def _is_safe_url(self, url: str) -> bool:
        """Check if the URL is safe (not an internal network address).

        Args:
            url: The URL to validate.

        Returns:
            True if the URL points to a safe public hostname.

        """
        try:
            parsed = urlparse(url)
            raw_hostname = parsed.hostname
        except ValueError:
            return False
        if parsed.scheme.lower() != "https":
            return False
        if not raw_hostname:
            return False

        hostname = normalize_hostname(raw_hostname)
        if hostname is None or is_special_use_hostname(hostname):
            return False

        if hostname in self._safe_hostname_cache:
            return self._safe_hostname_cache[hostname]

        async with self._safe_hostname_lock:
            if hostname in self._safe_hostname_cache:
                return self._safe_hostname_cache[hostname]

            result = await self._perform_safe_hostname_check(hostname)
            if len(self._safe_hostname_cache) < self._max_hostname_cache_size:
                self._safe_hostname_cache[hostname] = result
            return result

    async def _perform_safe_hostname_check(self, hostname: str) -> bool:
        """Perform the actual DNS lookup and safety validation.

        Args:
            hostname: The hostname or IP to check.

        Returns:
            True if the destination is a safe public IP.

        """
        return bool(await async_resolve_public_addresses(self.hass, hostname, 443))

    def _is_safe_path(self, path: str) -> bool:
        """Check if the path is within the blueprints' directory.

        Args:
            path: Filesystem path to validate.

        Returns:
            True if the path is safely contained within blueprints folder.

        """
        blueprint_path = self.hass.config.path(BLUEPRINTS_DATA_DIR)
        try:
            real_path = os.path.realpath(path)
            real_blueprints = os.path.realpath(blueprint_path)
            return os.path.commonpath([real_path, real_blueprints]) == real_blueprints
        except (ValueError, OSError):
            return False

    async def async_restore_blueprint(self, path: str, version: int = 1) -> dict[str, object]:
        """Serialize and restore a validated backup as one per-path transaction."""
        real_path = os.path.realpath(path)
        async with self._file_store.transaction(real_path):
            return await self._async_restore_blueprint_locked(path, version)

    async def _async_restore_blueprint_locked(
        self, path: str, version: int = 1
    ) -> dict[str, object]:
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
        max_backups = get_max_backups(self.config_entry)
        if validation_result := self._validate_blueprint_restore_request(
            real_path,
            version,
            max_backups,
        ):
            return validation_result

        try:
            prepared = await self._async_prepare_blueprint_restore(
                path,
                real_path,
                version,
            )
            if prepared is None:
                return self._blueprint_restore_result(False, "missing_backup")

            success, message, new_backups_count = await self.hass.async_add_executor_job(
                BlueprintUpdateCoordinator._execute_restore_file,
                real_path,
                version,
                max_backups,
                prepared.content,
                prepared.precondition,
            )
            if not success:
                _LOGGER.error(
                    "Failed to restore blueprint at %s: %s",
                    real_path,
                    message,
                )
                if message == _RESTORE_REVISION_MISMATCH:
                    return self._blueprint_restore_result(
                        False,
                        "system_error",
                        error=_LOCAL_REVISION_MISMATCH_ERROR,
                    )
                error = "Filesystem error during restoration" if message == "system_error" else None
                return self._blueprint_restore_result(False, message, error=error)

            await self._async_finalize_blueprint_restore(path, prepared, new_backups_count)
            return self._blueprint_restore_result(True, message)
        except BlueprintRestoreValidationError as err:
            _LOGGER.warning(
                "Rejected invalid backup for %s: %s",
                real_path,
                err.result_translation_key,
            )
            return self._blueprint_restore_result(
                False,
                err.result_translation_key,
                **err.translation_kwargs,
            )
        except FileRevisionMismatchError as err:
            _LOGGER.warning("Rejected stale blueprint restore at %s: %s", real_path, err)
            return self._blueprint_restore_result(
                False,
                "system_error",
                error=_LOCAL_REVISION_MISMATCH_ERROR,
            )
        except Exception as err:
            _LOGGER.exception("Failed to restore blueprint at %s", real_path)
            return self._blueprint_restore_result(False, "system_error", error=str(err))

    @staticmethod
    def _blueprint_restore_result(
        success: bool,
        translation_key: str,
        error: str | None = None,
        **translation_kwargs: str,
    ) -> dict[str, object]:
        """Build the translated service result for a restore attempt."""
        if error is not None:
            translation_kwargs["error"] = error
        return {
            "success": success,
            "translation_key": translation_key,
            "translation_kwargs": translation_kwargs,
        }

    def _validate_blueprint_restore_request(
        self,
        real_path: str,
        version: int,
        max_backups: int,
    ) -> dict[str, object] | None:
        """Return an error result for an unsafe or invalid restore request."""
        if not self._is_safe_path(real_path):
            _LOGGER.error("Security violation: Attempted to restore unsafe path: %s", real_path)
            return self._blueprint_restore_result(
                False,
                "system_error",
                error="Security violation: Attempted to restore unsafe path",
            )
        if version < 1 or version > max_backups:
            _LOGGER.error(
                "Invalid backup version %s requested for %s (current limit: %s)",
                version,
                real_path,
                max_backups,
            )
            return self._blueprint_restore_result(
                False,
                "invalid_version",
                version=str(version),
                max_backups=str(max_backups),
            )
        return None

    async def _async_prepare_blueprint_restore(
        self,
        path: str,
        real_path: str,
        version: int,
    ) -> PreparedBlueprintRestore | None:
        """Read and validate a backup before allowing filesystem mutation."""
        precondition = await self.hass.async_add_executor_job(
            self._file_store.capture_precondition,
            real_path,
        )
        try:
            backup_content = await self.hass.async_add_executor_job(
                self._file_store.read_backup,
                real_path,
                version,
            )
        except FileNotFoundError:
            _LOGGER.error(
                "Backup version %s requested for %s does not exist on disk",
                version,
                real_path,
            )
            return None

        parsed_backup = self._parse_blueprint_backup(backup_content)
        domain = self._get_functional_domain(real_path)
        source_info = self.data.get(path) or self.data.get(real_path) or {}
        tracked_source_url = source_info.get("source_url")
        parsed_dict: dict[str, object] | None = (
            {str(k): v for k, v in parsed_backup.items()}
            if isinstance(parsed_backup, dict)
            else None
        )
        bp_block = (
            self._get_blueprint_block(
                real_path,
                parsed_data=parsed_dict,
            )
            if parsed_dict
            else None
        )
        backup_source_url = bp_block.get("source_url") if bp_block else None
        if tracked_source_url and backup_source_url != tracked_source_url:
            raise BlueprintRestoreValidationError(
                "blueprint_validation_error",
                error="Backup source URL does not match the tracked blueprint",
            )

        src_url = tracked_source_url or backup_source_url or real_path
        src_url_str = str(src_url) if src_url else real_path
        bp_dict: dict[str, object] = (
            {str(k): v for k, v in parsed_backup.items()} if isinstance(parsed_backup, dict) else {}
        )
        if validation_error := self._validate_blueprint(
            bp_dict,
            src_url_str,
            domain,
        ):
            error_parts = split_error_message(validation_error)
            raise BlueprintRestoreValidationError(
                "blueprint_validation_error",
                error=(error_parts[1] if error_parts else validation_error.replace("_", " ")),
            )
        return PreparedBlueprintRestore(
            real_path=real_path,
            content=backup_content,
            domain=domain,
            tracked_source_url=tracked_source_url,
            precondition=precondition,
        )

    @staticmethod
    def _parse_blueprint_backup(backup_content: str) -> object:
        """Parse backup YAML and present a sanitized validation error."""
        try:
            return yaml_util.parse_yaml(backup_content)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Backup content is not valid YAML: %s",
                sanitize_error_detail(str(err)),
            )
            raise BlueprintRestoreValidationError("invalid_yaml") from err

    async def _async_finalize_blueprint_restore(
        self,
        path: str,
        prepared: PreparedBlueprintRestore,
        new_backups_count: int,
    ) -> None:
        """Synchronize restored state, reload its domain, and refresh data."""
        relative_path = get_blueprint_relative_path(self.hass, prepared.real_path)
        if relative_path and relative_path in self._persisted_metadata:
            entry = self._persisted_metadata[relative_path]
            entry["etag"] = None
            entry["remote_hash"] = None
            entry["last_modified"] = None

        data_path = path if path in self.data else prepared.real_path
        if data_path in self.data:
            restored_hash = self._hash_content(
                prepared.content,
                (
                    prepared.tracked_source_url
                    if isinstance(prepared.tracked_source_url, str)
                    else None
                ),
            )
            self.data[data_path].update(
                {
                    "etag": None,
                    "last_modified": None,
                    "local_hash": restored_hash,
                    "local_file_hash": hashlib.sha256(prepared.content.encode("utf-8")).hexdigest(),
                    "remote_hash": None,
                    "remote_content": None,
                    "updatable": False,
                    "backups_count": new_backups_count,
                    "_cached_git_diff": None,
                }
            )
            self.async_set_updated_data(self.data)
        await self.async_reconcile_reload_services([prepared.domain])
        try:
            await self.async_request_refresh()
        except Exception:
            _LOGGER.exception(
                "Blueprint restore committed, but the immediate refresh request failed"
            )

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
                c_semantic_bool = bool(c_semantic)
                return GitDiffResult(diff_text=c_diff, is_semantic_sync=c_semantic_bool)
        return None

    @staticmethod
    def _extract_inputs_schema(
        content: str,
    ) -> tuple[dict[str, dict[str, object]], str | None]:
        """Extract input schema from blueprint YAML content.

        Args:
            content: Raw YAML content of the blueprint.

        Returns:
            A dictionary mapping input names to their properties (mandatory, selector).
        """
        try:
            content_to_parse = BlueprintUpdateCoordinator._extract_blueprint_text(content)
            try:
                data = yaml_util.parse_yaml(content_to_parse)
            except HomeAssistantError:
                data = yaml_util.parse_yaml(content)
            if (
                not isinstance(data, dict)
                or "blueprint" not in data
                or not isinstance(data["blueprint"], dict)
            ):
                return {}, None
            inputs = data["blueprint"].get("input")
            if not isinstance(inputs, dict):
                return {}, None

            schema: dict[str, dict[str, object]] = {}

            def _process_inputs(input_dict: dict[str, object], *, top_level: bool = True) -> None:
                """Flatten inputs, recursing into sections (HA 2024.6+)."""
                for key, val in input_dict.items():
                    if top_level and key in TOP_LEVEL_SELECTOR_PRESENTATION_KEYS:
                        continue
                    if isinstance(val, dict):
                        if "input" in val:
                            input_val = val.get("input")
                            if isinstance(input_val, dict) and input_val:
                                _process_inputs(
                                    {str(k): v for k, v in input_val.items()},
                                    top_level=False,
                                )
                            continue
                        sel = val.get("selector")
                        if isinstance(sel, dict) and sel:
                            selector_name = str(next(iter(sel.keys())))
                            selector_value = sel.get(selector_name)
                            try:
                                validated_selector = validate_selector(sel)
                                selector_config = (
                                    BlueprintUpdateCoordinator._normalize_selector_config(
                                        validated_selector.get(selector_name) or {}
                                    )
                                )
                            except vol.Invalid:
                                selector_config = (
                                    BlueprintUpdateCoordinator._normalize_selector_config(
                                        selector_value
                                    )
                                )
                        else:
                            selector_name = None
                            selector_config = None
                        mandatory = "default" not in val
                        schema[key] = {
                            "mandatory": mandatory,
                            "selector": selector_name,
                            "selector_config": selector_config,
                        }
                    else:
                        schema[key] = {"mandatory": True, "selector": None, "selector_config": None}

            _process_inputs({str(k): v for k, v in inputs.items()})
            return schema, None
        except HomeAssistantError as err:
            _LOGGER.warning("Failed to extract inputs schema from blueprint")
            _LOGGER.debug("Failed to extract inputs schema from blueprint: %s", err)
            return {}, str(err)

    @staticmethod
    def _normalize_selector_config(value: object, *, top_level: bool = True) -> JSONValue:
        """Return a stable selector contract without cosmetic presentation keys."""
        if isinstance(value, dict):
            return {
                str(key): BlueprintUpdateCoordinator._normalize_selector_config(
                    item,
                    top_level=False,
                )
                for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
                if not top_level or key not in TOP_LEVEL_SELECTOR_PRESENTATION_KEYS
            }
        if isinstance(value, list):
            return [
                BlueprintUpdateCoordinator._normalize_selector_config(
                    item,
                    top_level=False,
                )
                for item in value
            ]
        if value is None or isinstance(value, bool | int | float | str):
            return value
        return str(value)

    def _get_entities_configs(self, entity_ids: list[str]) -> dict[str, dict[str, object]]:
        """Get input configurations for blueprint-based entities.

        Args:
            entity_ids: List of entity IDs to fetch configs for.

        Returns:
            A dictionary mapping entity IDs to their configured input values.

        """
        configs: dict[str, dict[str, object]] = {}
        remaining_ids = set(entity_ids)

        for domain in (FunctionalDomain.AUTOMATION, FunctionalDomain.SCRIPT):
            if not remaining_ids:
                break

            domain_ids = {eid for eid in remaining_ids if eid.startswith(f"{domain}.")}
            if not domain_ids:
                continue

            remaining_ids -= domain_ids

            if domain not in self.hass.data:
                continue

            component = self.hass.data[domain]
            get_entity = getattr(component, "get_entity", None)

            if callable(get_entity):
                for entity_id in domain_ids:
                    if entity := get_entity(entity_id):
                        self._populate_config_from_entity(entity, entity_id, configs)
            else:
                entities_attr = getattr(component, "entities", [])
                entity_map = {
                    getattr(e, "entity_id", None): e
                    for e in entities_attr
                    if getattr(e, "entity_id", None)
                }
                for entity_id in domain_ids:
                    if entity := entity_map.get(entity_id):
                        self._populate_config_from_entity(entity, entity_id, configs)

        if remaining_ids:
            for platform in async_get_platforms(self.hass, FunctionalDomain.TEMPLATE):
                for entity_id, entity in platform.entities.items():
                    if entity_id in remaining_ids:
                        self._populate_config_from_entity(entity, entity_id, configs)
                        remaining_ids.remove(entity_id)
                        if not remaining_ids:
                            return configs

        return configs

    @staticmethod
    def _populate_config_from_entity(
        entity: object, entity_id: str, configs: dict[str, dict[str, object]]
    ) -> None:
        """Extract and validate blueprint configuration from a HA entity.

        Attempts to recover blueprint inputs from available entity attributes.
        Prefers public attributes when they still contain the blueprint instance
        configuration, then falls back to the internal ``_blueprint_inputs``
        attribute used by Home Assistant Core integrations.

        Args:
            entity: The Home Assistant entity object.
            entity_id: The entity ID of the consumer.
            configs: Dictionary to store the extracted configurations.

        """
        candidates = (
            getattr(entity, "raw_config", None),
            getattr(entity, "config", None),
            getattr(entity, "_blueprint_inputs", None),
        )
        for candidate in candidates:
            if isinstance(candidate, dict) and "use_blueprint" in candidate:
                configs[entity_id] = candidate
                return

    @staticmethod
    def _get_affected_entities(configs: Mapping[str, Mapping[str, object]], key: str) -> list[str]:
        """Find entities using a specific input key."""
        return [eid for eid, inputs in configs.items() if key in inputs]

    @staticmethod
    def _is_input_mandatory(props: object) -> bool:
        """Check if an input schema property dictionary represents a mandatory input."""
        if not isinstance(props, dict):
            return True
        if "mandatory" in props:
            return bool(props.get("mandatory"))
        return "default" not in props

    @staticmethod
    def _detect_new_mandatory_inputs(
        old_schema: Mapping[str, object], new_schema: Mapping[str, object]
    ) -> list[StructuredRisk]:
        """Detect new mandatory inputs in the schema."""
        risks: list[StructuredRisk] = []
        for key, props in new_schema.items():
            if BlueprintUpdateCoordinator._is_input_mandatory(props):
                old_props = old_schema.get(key)
                old_mandatory = (
                    BlueprintUpdateCoordinator._is_input_mandatory(old_props)
                    if old_props is not None
                    else False
                )
                if not old_mandatory:
                    risks.append({"type": BlueprintRiskType.NEW_MANDATORY, "args": {"input": key}})
        return risks

    @staticmethod
    def _detect_missing_inputs(
        new_schema: Mapping[str, Mapping[str, object]],
        configs: Mapping[str, Mapping[str, object]],
    ) -> list[StructuredRisk]:
        """Detect missing mandatory inputs for existing entities."""
        risks: list[StructuredRisk] = []
        for entity_id, inputs in configs.items():
            risks.extend(
                {
                    "type": BlueprintRiskType.MISSING_INPUT,
                    "args": {"entity": entity_id, "input": key},
                }
                for key, props in new_schema.items()
                if isinstance(props, dict) and props.get("mandatory") and key not in inputs
            )
        return risks

    def _detect_selector_mismatches(
        self,
        old_schema: Mapping[str, Mapping[str, object]],
        new_schema: Mapping[str, Mapping[str, object]],
        configs: Mapping[str, Mapping[str, object]],
    ) -> list[StructuredRisk]:
        """Detect changes in selectors for existing inputs."""
        risks: list[StructuredRisk] = []
        for key in old_schema:
            if key in new_schema:
                old_val = old_schema[key]
                new_val = new_schema[key]
                old_props = old_val if isinstance(old_val, dict) else {}
                new_props = new_val if isinstance(new_val, dict) else {}
                old_selector = old_props.get("selector")
                new_selector = new_props.get("selector")
                old_config = old_props.get("selector_config")
                new_config = new_props.get("selector_config")
                if (old_selector != new_selector or old_config != new_config) and (
                    affected := self._get_affected_entities(configs, key)
                ):
                    if old_selector != new_selector:
                        risks.append(
                            {
                                "type": BlueprintRiskType.SELECTOR_MISMATCH,
                                "args": {
                                    "input": key,
                                    "old_type": str(old_selector) if old_selector else "none",
                                    "new_type": str(new_selector) if new_selector else "none",
                                    "count": len(affected),
                                },
                            }
                        )
                    else:
                        risks.append(
                            {
                                "type": BlueprintRiskType.SELECTOR_CONFIG_CHANGED,
                                "args": {
                                    "input": key,
                                    "type": str(old_selector) if old_selector else "unknown",
                                    "count": len(affected),
                                },
                            }
                        )
        return risks

    def _detect_removed_inputs(
        self,
        old_schema: Mapping[str, Mapping[str, object]],
        new_schema: Mapping[str, Mapping[str, object]],
        configs: Mapping[str, Mapping[str, object]],
    ) -> list[StructuredRisk]:
        """Detect inputs that were removed but are still used."""
        risks: list[StructuredRisk] = []
        for key in old_schema:
            if key not in new_schema and (affected := self._get_affected_entities(configs, key)):
                risks.append(
                    {
                        "type": BlueprintRiskType.REMOVED_INPUT,
                        "args": {"input": key, "count": len(affected)},
                    }
                )
        return risks

    @staticmethod
    def _dedupe_risks(risks: Iterable[StructuredRisk]) -> list[StructuredRisk]:
        """De-duplicate risks by type and arguments.

        This ensures that identical risks (by type and arguments) are only
        reported once, even if they originate from different detection passes.

        Args:
            risks: An iterable of structured risks.

        Returns:
            A list of unique structured risks.

        """
        seen: set[tuple[BlueprintRiskType, bytes]] = set()
        unique_risks: list[StructuredRisk] = []
        for risk in risks:
            if not isinstance(risk, dict) or "type" not in risk or "args" not in risk:
                _LOGGER.debug("Skipping malformed risk: %s", risk)
                continue

            key = (
                risk["type"],
                orjson.dumps(risk["args"], option=orjson.OPT_SORT_KEYS),
            )
            if key not in seen:
                seen.add(key)
                unique_risks.append(risk)
        return unique_risks

    def _detect_breaking_changes(
        self,
        old_content: str,
        new_content: str,
        configs: Mapping[str, Mapping[str, object]],
    ) -> list[StructuredRisk]:
        """Detect potential breaking changes between two versions of a blueprint.

        Args:
            old_content: Current local YAML content.
            new_content: Remote YAML content from update.
            configs: Precomputed map of entity_id -> input_map.

        Returns:
            A list of structured risks describing the detected changes and potential issues.

        """
        old_schema, old_error = self._extract_inputs_schema(old_content)
        if old_error:
            return [
                {
                    "type": BlueprintRiskType.VALIDATION_FAILED,
                    "args": {"error": old_error},
                }
            ]
        new_schema, new_error = self._extract_inputs_schema(new_content)
        if new_error:
            return [
                {
                    "type": BlueprintRiskType.VALIDATION_FAILED,
                    "args": {"error": new_error},
                }
            ]

        risks = []
        risks.extend(self._detect_new_mandatory_inputs(old_schema, new_schema))
        risks.extend(self._detect_missing_inputs(new_schema, configs))
        risks.extend(self._detect_selector_mismatches(old_schema, new_schema, configs))
        risks.extend(self._detect_removed_inputs(old_schema, new_schema, configs))

        return self._dedupe_risks(risks)

    def _get_blueprint_consumers(self, relative_path: str) -> list[str] | None:
        """Return unique entity IDs referencing this blueprint.

        Consumer discovery is resilient to non-standard or missing domain prefixes.

        """
        parts = relative_path.split("/", 1)
        domain = parts[0] if len(parts) > 1 else None
        bp_id = parts[-1] if len(parts) > 1 else relative_path
        return get_blueprint_usage_entities(self.hass, domain, bp_id)

    def _get_entities_using_blueprint(self, relative_path: str) -> list[str] | None:
        """Get entity IDs of automations, scripts, and templates using the given blueprint."""
        return self._get_blueprint_consumers(relative_path)

    async def _async_validate_blueprint_consumers(
        self,
        relative_path: str,
        blueprint_content: str,
        configs: dict[str, dict[str, object]],
    ) -> list[StructuredRisk]:
        """Validate all consumers of a blueprint against specific content.

        This uses Home Assistant's native input substitution and domain
        validators without publishing candidate content to the shared hub.

        Args:
            relative_path: Relative path of the blueprint.
            blueprint_content: Raw YAML content for validation.
            configs: Current configurations of all affected entities.

        Returns:
            A list of compatibility risks or system errors if validation fails.

        """
        risks: list[StructuredRisk] = []
        try:
            blueprint_dict = yaml_util.parse_yaml(blueprint_content)
            if not isinstance(blueprint_dict, dict):
                return [
                    {
                        "type": BlueprintRiskType.VALIDATION_FAILED,
                        "args": {"error": f"{relative_path}: Not a dictionary"},
                    }
                ]

            parts = relative_path.split("/", 1)
            if len(parts) < 2:
                return [
                    {
                        "type": BlueprintRiskType.SYSTEM_ERROR,
                        "args": {
                            "error": (
                                f"Malformed blueprint path: missing domain "
                                f"folder in '{relative_path}'"
                            ),
                            "path": relative_path,
                        },
                    }
                ]
            domain = parts[0]
            schema = BlueprintUpdateCoordinator._get_blueprint_schema(domain)

            blueprint_obj = Blueprint(
                blueprint_dict, expected_domain=domain, path=relative_path, schema=schema
            )
        except HomeAssistantError as err:
            return [
                {
                    "type": BlueprintRiskType.VALIDATION_FAILED,
                    "args": {"error": sanitize_error_detail(str(err))},
                }
            ]
        except Exception as err:
            safe_error = sanitize_error_detail(str(err))
            _LOGGER.exception("Unexpected error during blueprint validation for %s", relative_path)
            return [
                {
                    "type": BlueprintRiskType.SYSTEM_ERROR,
                    "args": {"error": safe_error, "path": relative_path},
                }
            ]

        async with self._blueprint_validate_lock:
            for entity_id, config in configs.items():
                try:
                    blueprint_inputs = BlueprintInputs(blueprint_obj, config)
                    blueprint_inputs.validate()
                    substituted_config = blueprint_inputs.async_substitute()
                    if domain == FunctionalDomain.AUTOMATION:
                        await async_validate_automation_config(
                            self.hass,
                            config_key=entity_id,
                            config=substituted_config,
                        )
                    elif domain == FunctionalDomain.TEMPLATE:
                        await async_validate_template_config(
                            self.hass,
                            config=substituted_config,
                        )
                    else:
                        object_id = (
                            entity_id.split(".", 1)[1]
                            if entity_id.startswith("script.") and "." in entity_id
                            else entity_id
                        )
                        await async_validate_script_config(
                            self.hass,
                            object_id=object_id,
                            config=substituted_config,
                        )
                except (HomeAssistantError, vol.Invalid) as err:
                    risks.append(
                        {
                            "type": BlueprintRiskType.COMPATIBILITY,
                            "args": {
                                "entity": entity_id,
                                "error": sanitize_error_detail(str(err)),
                            },
                        }
                    )

        return risks

    async def async_summarize_risks(self, risks: Iterable[Mapping[str, object]]) -> str:
        """Create a localized newline-separated string of risks.

        This shared helper provides a consistent formatting and translation
        path for risks displayed in both UI release notes and persistent
        notifications.

        Args:
            risks: List of structured risks to summarize.

        Returns:
            A formatted string with bullet points for each risk.
        """

        async def _translate_risk(risk: Mapping[str, object]) -> str:
            """Translate a single risk to a bullet-point string."""
            rtype_raw = risk.get("type")
            if isinstance(rtype_raw, BlueprintRiskType):
                rtype: object = rtype_raw
            elif isinstance(rtype_raw, str):
                try:
                    rtype = BlueprintRiskType(rtype_raw)
                except ValueError:
                    rtype = rtype_raw
            else:
                rtype = BlueprintRiskType.SYSTEM_ERROR

            rargs_raw = risk.get("args")
            rargs = dict(rargs_raw) if isinstance(rargs_raw, dict) else {}

            translation_key = (
                RISK_TYPE_TRANSLATIONS.get(rtype) if isinstance(rtype, BlueprintRiskType) else None
            )

            if translation_key is None:
                translation_key = "risk_unknown"
                rargs.pop("type", None)
                rargs.setdefault("error", str(rtype or risk))
                rargs_str = {k: str(v) for k, v in rargs.items()}
                if rtype:
                    translated = await self.async_translate(
                        translation_key, type=str(rtype), **rargs_str
                    )
                    return f"- {translated}"
                translated = await self.async_translate(translation_key, **rargs_str)
                return f"- {translated}"

            rargs_str = {k: str(v) for k, v in rargs.items()}
            return f"- {await self.async_translate(translation_key, **rargs_str)}"

        lines = await asyncio.gather(*[_translate_risk(r) for r in risks])
        return "\n".join(lines)

    def set_cached_git_diff(
        self,
        path: str,
        local_hash: str | None,
        remote_hash: str | None,
        diff_text: str,
        is_semantic_sync: bool = False,
    ) -> None:
        """Set cached git diff.

        If the coordinator's data is not yet initialized or
        the path is not in the data, this method does nothing.

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
        """Fetch and validate remote content for diff generation.

        This method mutates the blueprint's `info` dictionary by setting
        the `remote_content` key ONLY if the URL is safe and the content
        passes blueprint validation. This prevents unvalidated content
        from being used in the installation flow.
        """
        info = self.data.get(path)
        if not info or not info.get("updatable"):
            return None

        source_url_val = info.get("source_url")
        source_url = source_url_val if isinstance(source_url_val, str) else ""
        normalized_url = normalize_url(source_url)
        if not normalized_url:
            return None

        if not await self._is_safe_url(normalized_url):
            _LOGGER.warning("Blocking diff fetch from unsafe URL: %s", redact_url(normalized_url))
            self._update_error_state(path, "unsafe_url", source_url)
            return None

        session = get_guarded_async_client(self.hass, **self._get_client_kwargs())
        remote_content, _, _ = await self._async_fetch_content(
            session,
            normalized_url,
            etag=None,
            last_modified=None,
            force=True,
        )

        if not remote_content:
            return None

        remote_content_with_url = self._ensure_source_url(remote_content, source_url)
        try:
            remote_parsed = yaml_util.parse_yaml(remote_content_with_url)
            blueprint_dict: dict[str, object] = (
                {str(k): v for k, v in remote_parsed.items()}
                if isinstance(remote_parsed, dict)
                else {}
            )
            expected_domain = self._get_functional_domain(path)
            last_error = self._validate_blueprint(
                blueprint_dict, source_url, expected_domain=expected_domain
            )
        except (HomeAssistantError, InvalidBlueprint) as err:
            last_error = format_error_message("yaml_syntax_error", err)

        if last_error:
            _LOGGER.warning("Remote content for diff at %s is invalid: %s", path, last_error)
            info["last_error"] = last_error
            return None

        info["last_error"] = None
        info["remote_content"] = remote_content_with_url
        return remote_content_with_url

    async def async_get_git_diff(self, path: str) -> GitDiffResult | None:
        """Get or generate git diff for a blueprint.

        This method orchestrates the entire diff generation process:
        1. Checks for a valid cached diff.
        2. Fetches remote content if missing (mutates state).
        3. Generates the diff in an executor job.
        4. Updates and returns the cached diff.

        Returns:
            GitDiffResult or None if it cannot be generated.
        """
        if path not in self.data:
            return None

        info = self.data[path]
        local_hash = info.get("local_hash")
        remote_hash = info.get("remote_hash")
        source_url = info.get("source_url")

        if remote_hash is None and info.get("updatable"):
            _LOGGER.error(
                "Internal error: Attempted to generate diff for updatable "
                "blueprint with None remote_hash at %s",
                path,
            )
            return None

        source_url_str = str(source_url) if source_url else ""
        local_hash_str = str(local_hash) if local_hash else None
        remote_hash_str = str(remote_hash) if remote_hash else None
        if (result := self.get_cached_git_diff(path, local_hash_str, remote_hash_str)) is not None:
            return result

        remote_content = info.get("remote_content")
        if remote_content is None and info.get("updatable"):
            try:
                remote_content = await self.async_fetch_diff_content(path)
            except Exception as err:
                _LOGGER.warning(
                    "Context fetch failed for diff at %s: %s",
                    path,
                    sanitize_error_detail(str(err)),
                )
                return None

        if not isinstance(remote_content, str) or not remote_content:
            return None

        try:
            diff_text = await self.hass.async_add_executor_job(
                BlueprintUpdateCoordinator._read_and_diff,
                path,
                remote_content,
                source_url_str,
            )
        except Exception as err:
            _LOGGER.warning("Failed to generate diff for %s: %s", path, err)
            return None

        remote_content_str = remote_content if isinstance(remote_content, str) else ""
        is_semantic_sync = self._is_semantically_equal(
            remote_content_str, local_hash_str or "", source_url_str
        )
        self.set_cached_git_diff(
            path, local_hash_str, remote_hash_str, diff_text or "", is_semantic_sync
        )
        return GitDiffResult(diff_text=diff_text or "", is_semantic_sync=is_semantic_sync)

    def is_auto_update_enabled(self) -> bool:
        """Return whether auto-update is enabled.

        Checks configuration options and falls back to the system default.

        Returns:
            Boolean indicating auto-update preference.

        """
        return get_config_bool(self.config_entry, CONF_AUTO_UPDATE, DEFAULT_AUTO_UPDATE)

    @classmethod
    def get_coordinator_for_flow(
        cls,
        hass: HomeAssistant,
        config_entry_id: str | None = None,
    ) -> Self | None:
        """Find the relevant coordinator instance for a repair or options flow."""
        domain_data = hass.data.get(DOMAIN)
        if not isinstance(domain_data, dict):
            return None
        coordinators_map = domain_data.get("coordinators")
        if not isinstance(coordinators_map, dict):
            return None

        if config_entry_id:
            candidate = coordinators_map.get(config_entry_id)
            return candidate if isinstance(candidate, cls) else None

        coordinators = list(coordinators_map.values())
        if len(coordinators) == 1 and isinstance(coordinators[0], cls):
            return coordinators[0]
        return None

    @staticmethod
    def get_withdrawn_issue_id(
        relative_path: str, domain: str | FunctionalDomain | None = None
    ) -> str:
        """Return the deterministic repair issue ID for a withdrawn blueprint."""
        normalized_rel = relative_path.replace("\\", "/").strip("/")
        norm_domain: str | None = None
        if isinstance(domain, FunctionalDomain):
            norm_domain = domain.value
        elif isinstance(domain, str) and domain.strip():
            norm_domain = domain.strip().lower()

        if norm_domain and not normalized_rel.startswith(f"{norm_domain}/"):
            normalized_rel = f"{norm_domain}/{normalized_rel}"
        path_hash = hashlib.sha256(normalized_rel.encode("utf-8")).hexdigest()[:16]
        return f"{RepairIssueType.WITHDRAWN_BLUEPRINT}_{path_hash}"

    def _async_create_withdrawn_issue(
        self,
        path: str,
        info: Mapping[str, object],
        status_code: int = 404,
    ) -> None:
        """Raise a Home Assistant repair issue for a withdrawn blueprint."""
        relative_path_obj = info.get("relative_path")
        relative_path = (
            str(relative_path_obj)
            if relative_path_obj
            else get_blueprint_relative_path(self.hass, path)
        )
        if not relative_path:
            return

        name = str(info.get("name") or relative_path)
        source_url = str(info.get("source_url") or "")
        domain_obj = info.get("domain")
        domain = normalize_domain(domain_obj) if domain_obj else None
        domain_str = domain.value if domain else ""

        issue_id = self.get_withdrawn_issue_id(relative_path, domain)
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=RepairIssueType.WITHDRAWN_BLUEPRINT,
            translation_placeholders={
                "name": name,
                "path": relative_path,
                "source_url": redact_url(source_url),
                "domain": domain_str,
                "status_code": str(status_code),
            },
            data={
                "config_entry_id": self.config_entry.entry_id if self.config_entry else None,
                "path": path,
                "relative_path": relative_path,
                "domain": domain_str,
                "name": name,
                "source_url": source_url,
            },
        )

    def _async_delete_withdrawn_issue(self, path: str) -> None:
        """Delete any repair issue for the given blueprint path."""
        info = self.data.get(path, {})
        relative_path_obj = info.get("relative_path")
        domain_obj = info.get("domain")
        domain = normalize_domain(domain_obj) if domain_obj else None
        relative_path = (
            str(relative_path_obj)
            if relative_path_obj
            else get_blueprint_relative_path(self.hass, path)
        )
        if relative_path:
            self._async_delete_withdrawn_issue_by_relative_path(relative_path, domain)

    def _async_delete_withdrawn_issue_by_relative_path(
        self, relative_path: str, domain: str | FunctionalDomain | None = None
    ) -> None:
        """Delete repair issue by relative path and optional domain."""
        if (
            domain is None
            and relative_path in self._persisted_metadata
            and (domain_val := self._persisted_metadata[relative_path].get("domain"))
        ):
            domain = normalize_domain(domain_val)
        issue_id = self.get_withdrawn_issue_id(relative_path, domain)
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _update_error_state(
        self, path: str, error_type: str, detail: object, clear_etag: bool = False
    ) -> None:
        """Update the blueprint state with a specific error.

        Args:
            path: Local path of the blueprint.
            error_type: Category of the error (e.g., 'fetch_error').
            detail: Detailed error information or exception.
            clear_etag: If True, clear the stored ETag for this blueprint.

        """
        if path in self.data:
            update_data = {
                "remote_hash": None,
                "remote_content": None,
                "updatable": False,
                "last_error": format_error_message(error_type, detail),
                "invalid_remote_hash": None,
                "update_blocking_reason": None,
                "breaking_risks": [],
            }
            if clear_etag:
                update_data["etag"] = None

            self.data[path].update(update_data)
        else:
            _LOGGER.warning("Attempted to update error state for missing blueprint path: %s", path)

    async def _async_update_blueprint_in_place(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: Mapping[str, object],
        results_to_notify: list[str],
        updated_domains: set[str],
        force: bool = False,
        refresh_work: RefreshWorkItem | None = None,
    ) -> None:
        """Update a single blueprint directly in self.data."""
        if not self._is_current_refresh_item(refresh_work):
            return
        source_url_obj = info.get("source_url")
        if not isinstance(source_url_obj, str) or not source_url_obj:
            return
        source_url = source_url_obj

        if not await self._is_safe_url(source_url):
            if not self._is_current_refresh_item(refresh_work):
                return
            _LOGGER.warning("Blocking update from untrusted URL: %s", redact_url(source_url))
            self._update_error_state(path, "unsafe_url", source_url, clear_etag=True)
            return

        normalized_url = normalize_url(source_url)
        if not normalized_url:
            return

        if not await self._is_safe_url(normalized_url):
            if not self._is_current_refresh_item(refresh_work):
                return
            _LOGGER.warning("Blocking update from untrusted URL: %s", redact_url(normalized_url))
            self._update_error_state(path, "unsafe_url", source_url, clear_etag=True)
            return

        stored_etag = self.data.get(path, {}).get("etag")
        stored_last_modified = self.data.get(path, {}).get("last_modified")
        stored_remote_hash = self.data.get(path, {}).get("remote_hash")

        etag = str(stored_etag) if (stored_remote_hash and not force and stored_etag) else None
        last_modified = (
            str(stored_last_modified)
            if (stored_remote_hash and not force and stored_last_modified)
            else None
        )

        try:
            remote_content, new_etag, new_last_modified = await self._async_fetch_content(
                session,
                normalized_url,
                etag=etag,
                last_modified=last_modified,
                force=force,
            )

            if remote_content is None:
                remote_content, new_etag, new_last_modified = await self._handle_not_modified_case(
                    session,
                    path,
                    info,
                    normalized_url,
                    new_etag,
                    new_last_modified,
                    refresh_work,
                )
        except (TimeoutError, httpx.HTTPError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Failed to fetch blueprint from %s: %s",
                redact_url(source_url),
                sanitize_error_detail(str(err)),
            )
            if self._is_current_refresh_item(refresh_work):
                if isinstance(err, httpx.HTTPStatusError) and err.response.status_code in (
                    HTTPStatus.NOT_FOUND,
                    HTTPStatus.GONE,
                ):
                    self._async_create_withdrawn_issue(
                        path, info, status_code=err.response.status_code
                    )
                    self._update_error_state(
                        path, "withdrawn_blueprint_error", err, clear_etag=True
                    )
                else:
                    self._update_error_state(path, "fetch_error", err)
            return

        if not self._is_current_refresh_item(refresh_work):
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
                source_url,
                results_to_notify,
                updated_domains,
                new_etag=new_etag,
                new_last_modified=new_last_modified,
                refresh_work=refresh_work,
            )
        except Exception as err:
            _LOGGER.error(
                "Error processing blueprint from %s: %s",
                redact_url(source_url),
                sanitize_error_detail(str(err)),
            )
            if self._is_current_refresh_item(refresh_work):
                self._update_error_state(path, "processing_error", err, clear_etag=True)
            return

    async def _handle_not_modified_case(
        self,
        session: httpx.AsyncClient,
        path: str,
        info: Mapping[str, object],
        normalized_url: str,
        new_etag: str | None = None,
        new_last_modified: str | None = None,
        refresh_work: RefreshWorkItem | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Handle the 304 Not Modified case for a blueprint."""
        if not self._is_current_refresh_item(refresh_work):
            return None, new_etag, new_last_modified
        _LOGGER.debug("[304] '%s' is up to date on server", info.get("name"))
        if not (self.data and path in self.data):
            return None, new_etag, new_last_modified

        self._async_delete_withdrawn_issue(path)

        if new_etag:
            self.data[path]["etag"] = new_etag

        if new_last_modified:
            self.data[path]["last_modified"] = new_last_modified

        remote_hash = self.data[path].get("remote_hash")
        if not remote_hash:
            return None, new_etag, new_last_modified

        local_hash = info.get("local_hash")
        self.data[path]["updatable"] = local_hash != remote_hash

        if self.data[path]["updatable"] and self.is_auto_update_enabled():
            _LOGGER.debug(
                "Auto-update enabled for '%s', fetching on-demand",
                info.get("name"),
            )
            return await self._async_fetch_content(
                session,
                normalized_url,
                etag=None,
                last_modified=None,
                force=True,
            )

        return None, new_etag, new_last_modified

    async def _process_blueprint_content(
        self,
        path: str,
        info: Mapping[str, object],
        remote_content: str,
        source_url: str,
        results_to_notify: list[str],
        updated_domains: set[str],
        new_etag: str | None = None,
        new_last_modified: str | None = None,
        refresh_work: RefreshWorkItem | None = None,
    ) -> None:
        """Process and validate newly fetched blueprint content."""
        if not self._is_current_refresh_item(refresh_work):
            return
        real_path = os.path.realpath(path)
        source_url_str = source_url if isinstance(source_url, str) else ""
        remote_content_with_url = remote_content
        try:
            remote_content_with_url = self._ensure_source_url(remote_content, source_url_str)
            remote_parsed = yaml_util.parse_yaml(remote_content_with_url)
            expected_domain = self._get_functional_domain(real_path)
            blueprint_dict: dict[str, object] = (
                {str(k): v for k, v in remote_parsed.items()}
                if isinstance(remote_parsed, dict)
                else {}
            )
            if validation_error := self._validate_blueprint(
                blueprint_dict, source_url_str, expected_domain=expected_domain
            ):
                updatable = False
                remote_hash = None
                last_error = validation_error
            else:
                remote_hash = self._hash_content(remote_content, source_url_str)
                local_hash = info.get("local_hash")
                updatable = bool(remote_hash and remote_hash != local_hash)
                last_error = None
        except (HomeAssistantError, InvalidBlueprint) as err:
            _LOGGER.warning(
                "Invalid blueprint content from %s: %s",
                redact_url(source_url_str),
                sanitize_error_detail(str(err)),
            )
            updatable = False
            remote_hash = None
            last_error = format_error_message("yaml_syntax_error", err)
        except Exception as err:
            _LOGGER.exception("Unexpected error processing blueprint for %s", path)
            updatable = False
            remote_hash = None
            last_error = format_error_message("processing_error", err)

        if not last_error:
            self._async_delete_withdrawn_issue(path)

        risks = await self._detect_risks_for_update(path, info, remote_content)
        if not self._is_current_refresh_item(refresh_work):
            return
        if self.data and path in self.data:
            self.data[path]["breaking_risks"] = risks

        if updatable and not last_error and self.is_auto_update_enabled():
            auto_update_handled = await self._handle_auto_update_step(
                path,
                info,
                remote_content_with_url,
                risks or [],
                results_to_notify,
                updated_domains,
                remote_hash=remote_hash,
                new_etag=new_etag,
                new_last_modified=new_last_modified,
                source_url=source_url_str,
                refresh_work=refresh_work,
            )
            if auto_update_handled or (
                self.data and path in self.data and self.data[path].get("update_blocking_reason")
            ):
                return

        self._update_coordinator_status_data(
            path,
            updatable,
            remote_content_with_url,
            risks=risks,
            last_error=last_error,
            remote_hash=remote_hash,
            new_etag=new_etag,
            new_last_modified=new_last_modified,
        )

    async def _detect_risks_for_update(
        self,
        path: str,
        info: Mapping[str, object],
        remote_content: str,
        session: httpx.AsyncClient | None = None,
    ) -> list[StructuredRisk]:
        """Detect potential breaking changes for a blueprint update."""
        risks = []
        relative_path_obj = info.get("relative_path")
        if not isinstance(relative_path_obj, str) or not relative_path_obj:
            _LOGGER.warning(
                "Missing relative path for blueprint at %s, skipping risk detection", path
            )
            return [
                {
                    "type": BlueprintRiskType.SYSTEM_ERROR,
                    "args": {"error": "missing_path", "path": os.path.basename(path)},
                }
            ]
        relative_path = relative_path_obj

        if self.data and path in self.data:
            local_file = self.hass.config.path(BLUEPRINTS_DATA_DIR, relative_path)
            try:
                entity_ids = self._get_entities_using_blueprint(relative_path)
                if entity_ids is None:
                    raise HomeAssistantError("Could not determine blueprint consumers")
                full_configs = self._get_entities_configs(entity_ids)
                configs: dict[str, dict[str, object]] = {}
                for eid, cfg in full_configs.items():
                    use_blueprint = cfg.get("use_blueprint") if isinstance(cfg, dict) else None
                    inp = use_blueprint.get("input") if isinstance(use_blueprint, dict) else None
                    configs[eid] = (
                        {str(k): v for k, v in inp.items()} if isinstance(inp, dict) else {}
                    )

                old_content = await self.hass.async_add_executor_job(read_local_file, local_file)
                if old_content:
                    risks = self._detect_breaking_changes(old_content, remote_content, configs)

                compatibility_risks = await self._async_validate_blueprint_consumers(
                    relative_path, remote_content, full_configs
                )
                risks.extend(compatibility_risks)

            except (OSError, HomeAssistantError) as err:
                safe_error = sanitize_error_detail(str(err))
                _LOGGER.warning(
                    "Failed to check breaking changes for %s (%s): %s",
                    path,
                    relative_path,
                    safe_error,
                )
                risks.append(
                    {
                        "type": BlueprintRiskType.SYSTEM_ERROR,
                        "args": {
                            "error": safe_error,
                            "path": relative_path,
                        },
                    }
                )
            except Exception as err:
                safe_error = sanitize_error_detail(str(err))
                _LOGGER.exception(
                    "Unexpected error checking breaking changes for %s (%s)", path, relative_path
                )
                risks.append(
                    {
                        "type": BlueprintRiskType.SYSTEM_ERROR,
                        "args": {
                            "error": safe_error,
                            "path": relative_path,
                        },
                    }
                )
        return self._dedupe_risks(risks)

    async def _handle_auto_update_step(
        self,
        path: str,
        info: Mapping[str, object],
        remote_content: str,
        risks: list[StructuredRisk],
        results_to_notify: list[str],
        updated_domains: set[str],
        remote_hash: str | None = None,
        new_etag: str | None = None,
        new_last_modified: str | None = None,
        source_url: str | None = None,
        refresh_work: RefreshWorkItem | None = None,
    ) -> bool:
        """Execute auto-update flow if safe."""
        if remote_hash is None:
            _LOGGER.error(
                "Internal error: Attempted auto-update with None remote_hash for %s", path
            )
            return False
        if not self._is_current_refresh_item(refresh_work):
            return True

        relative_path_obj = info.get("relative_path")
        relative_path = relative_path_obj if isinstance(relative_path_obj, str) else None
        in_use_entities = self._get_entities_using_blueprint(relative_path) if relative_path else []
        guard_failed = in_use_entities is None or any(
            risk.get("type") == BlueprintRiskType.SYSTEM_ERROR for risk in risks
        )
        is_breaking = bool(risks) and (guard_failed or bool(in_use_entities))

        if is_breaking:
            await self._async_handle_auto_update_blocked(
                path,
                info,
                remote_hash,
                remote_content,
                risks,
                guard_failed,
                new_etag=new_etag,
                new_last_modified=new_last_modified,
            )
            return True

        local_file_hash = info.get("local_file_hash")
        if not isinstance(local_file_hash, str):
            _LOGGER.error("Cannot auto-update %s without a scanned local file hash", path)
            return False

        try:
            await self.async_install_blueprint(
                path,
                remote_content,
                reload_services=False,
                backup=True,
                remote_hash=remote_hash,
                etag=new_etag,
                last_modified=new_last_modified,
                is_auto_update=True,
                source_url=source_url,
                file_precondition=FileRevisionPrecondition.existing(local_file_hash),
                refresh_work=refresh_work,
            )
            name_val = info.get("name")
            if isinstance(name_val, str):
                results_to_notify.append(name_val)
            domain_val = info.get("domain")
            updated_domains.add(str(domain_val) if domain_val else FunctionalDomain.AUTOMATION)
            return True
        except BlueprintRefreshObsoleteError:
            _LOGGER.debug("Skipping obsolete auto-update for %s", path)
            return True
        except Exception as err:
            _LOGGER.exception("Auto-update failed for %s", path)
            if self.data and path in self.data:
                self.data[path].update(
                    {
                        "updatable": True,
                        "remote_hash": remote_hash,
                        "remote_content": remote_content,
                        "invalid_remote_hash": None,
                        "update_blocking_reason": BlueprintBlockingReason.SYSTEM_ERROR,
                        "etag": new_etag,
                        "last_modified": new_last_modified,
                        "auto_update_last_error": sanitize_error_detail(str(err)),
                    }
                )
            return False

    async def _async_handle_auto_update_blocked(
        self,
        path: str,
        info: Mapping[str, object],
        remote_hash: str,
        remote_content: str,
        risks: list[StructuredRisk],
        guard_failed: bool,
        new_etag: str | None = None,
        new_last_modified: str | None = None,
    ) -> None:
        """Handle notification and state when auto-update is blocked."""
        bp_name = str(info.get("name", "Unknown"))
        _LOGGER.warning(
            "Auto-update blocked for '%s' due to %d detected breaking changes.",
            bp_name,
            len(risks),
        )
        title_key = (
            "auto_update_blocked_by_system_error"
            if guard_failed
            else "auto_update_blocked_by_breaking_change"
        )
        title = await self.async_translate(title_key, name=bp_name)
        risk_summary = await self.async_summarize_risks(risks)
        message = await self.async_translate(
            "breaking_risks_report", name=bp_name, risks=risk_summary
        )
        relative_path_obj = info.get("relative_path")
        relative_path = relative_path_obj if isinstance(relative_path_obj, str) else None
        await self._async_send_auto_update_notification(
            title,
            message,
            source_unique_id=slugify(relative_path) if relative_path else None,
        )
        if self.data and path in self.data:
            blocking_reason = (
                BlueprintBlockingReason.SYSTEM_ERROR
                if guard_failed
                else BlueprintBlockingReason.BREAKING_CHANGE
            )
            self.data[path].update(
                {
                    "updatable": True,
                    "remote_hash": remote_hash,
                    "remote_content": remote_content,
                    "last_error": None,
                    "invalid_remote_hash": None,
                    "update_blocking_reason": blocking_reason,
                    "etag": new_etag,
                    "last_modified": new_last_modified,
                }
            )

    def _update_coordinator_status_data(
        self,
        path: str,
        updatable: bool,
        remote_content: str,
        risks: list[StructuredRisk] | None = None,
        last_error: str | None = None,
        remote_hash: str | None = None,
        new_etag: str | None = None,
        new_last_modified: str | None = None,
    ) -> None:
        """Update internal data state for a blueprint."""
        if not (self.data and path in self.data):
            return

        if last_error:
            update_data: dict[str, object] = {
                "last_error": last_error,
                "etag": new_etag,
                "last_modified": new_last_modified,
                "invalid_remote_hash": remote_hash,
                "remote_hash": None,
                "remote_content": None,
                "updatable": False,
                "update_blocking_reason": None,
            }
        else:
            if updatable and remote_hash is None:
                _LOGGER.error(
                    "Internal error: Blueprint %s marked as updatable "
                    "but received None remote_hash",
                    path,
                )
            update_data: dict[str, object] = {
                "last_error": last_error,
                "etag": new_etag,
                "last_modified": new_last_modified,
                "invalid_remote_hash": None,
                "remote_hash": remote_hash,
                "remote_content": remote_content if updatable else None,
                "updatable": updatable,
                "update_blocking_reason": None,
                "auto_update_last_error": None,
            }

        if risks is None:
            existing_risks = self.data[path].get("breaking_risks")
            final_risks: list[Mapping[str, object]] = (
                [{str(k): v for k, v in r.items()} for r in existing_risks if isinstance(r, dict)]
                if isinstance(existing_risks, list)
                else []
            )
        else:
            final_risks = list(risks)
        update_data["breaking_risks"] = final_risks

        self.data[path].update(update_data)

    async def _async_send_auto_update_notification(
        self,
        title: str,
        message: str,
        source_unique_id: str | None = None,
    ) -> None:
        """Send a persistent notification to the Home Assistant UI.

        Args:
            title: Title of the notification.
            message: Content of the notification.
            source_unique_id: Stable, unique identifier for the blueprint source.

        """
        base_id = f"{DOMAIN}_auto_update_block"
        if source_unique_id:
            base_id = f"{base_id}_{source_unique_id}"
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": base_id,
                },
            )
        except Exception:
            _LOGGER.exception("Failed to send auto-update notification")

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
        last_modified: str | None = None,
        force: bool = False,
    ) -> tuple[str | None, str | None, str | None]:
        """Fetch content from a URL.

        Returns (content, etag, last_modified). Content is None on 304 Not Modified.

        Args:
            session: Async HTTP client.
            url: URL to fetch.
            etag: Optional ETag for conditional GET.
            last_modified: Optional Last-Modified for conditional GET.
            force: If True, bypass headers (even if provided) and force download.

        """
        headers: dict[str, str] = {}
        if not force:
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        await self._apply_request_pacing(url)

        _LOGGER.debug("[Pacing] Dispatching request for %s", redact_url(url))

        response = await self._execute_with_redirect_guard(session, url, headers)
        new_etag = response.headers.get("ETag") or etag
        new_last_modified = response.headers.get("Last-Modified") or last_modified
        content = await self._parse_provider_response(response, url)
        return content, new_etag, new_last_modified

    @property
    def _last_request_time(self) -> float:
        """Compatibility property for tests."""
        return max(self._last_request_times.values(), default=0.0)

    @_last_request_time.setter
    def _last_request_time(self, val: float) -> None:
        """Compatibility property setter for tests."""
        self._last_request_times.clear()
        self._last_request_times["_default_"] = val

    async def _apply_request_pacing(self, url: str) -> None:
        """Enforce a random pacing delay between outbound HTTP requests per domain.

        Acquires the pacing lock to calculate a safe send time based on the
        last request timestamp for the target host, then releases the lock
        before sleeping. This keeps concurrent coroutines from sleeping inside
        the lock.

        """
        parsed = urlparse(url)
        domain = parsed.netloc.lower() if parsed.netloc else "unknown"
        async with self._pacing_lock:
            now = time.monotonic()
            if len(self._last_request_times) > 100:
                self._last_request_times = {
                    k: v
                    for k, v in self._last_request_times.items()
                    if k == "_default_" or now - v < 3600
                }
            interval = random.uniform(MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
            last_time = self._last_request_times.get(
                domain, self._last_request_times.get("_default_", 0.0)
            )
            start_time = max(now, last_time + interval)
            delay = start_time - now
            self._last_request_times[domain] = start_time

        if delay > 0:
            await asyncio.sleep(delay)

    async def _execute_with_redirect_guard(
        self,
        session: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Perform the HTTP GET with manual redirect following and safety checks.

        Follows up to 20 redirects, validating each new location against the
        safe-hostname allowlist. Deterministic policy violations raise
        BlueprintFetchPolicyError and are not retried.

        Note on 304 responses:
        HTTP 304 Not Modified is technically in the 3xx Redirection class but
        represents a terminal state (conditional success) rather than a resource
        to be followed. This method treats 304 as a final response to ensure it
        is subjected to HTTPS enforcement and to avoid HTTPStatusErrors that
        some client versions raise for unhandled 3xx codes when
        follow_redirects=False.

        Args:
            session: Async HTTP client.
            url: Original request URL.
            headers: Request headers (e.g. If-None-Match).

        Returns:
            The final httpx.Response.

        """
        current_url = url
        current_headers = headers.copy()

        max_redirects = 20
        for _redirect_attempt in range(max_redirects):
            if urlparse(current_url).scheme.lower() != "https" or not await self._is_safe_url(
                current_url
            ):
                _LOGGER.warning("Blocking unsafe URL before request: %s", redact_url(current_url))
                raise BlueprintFetchPolicyError(
                    f"Security violation: Unsafe URL {redact_url(current_url)}"
                )

            response = await self._async_get_bounded_response(
                session,
                current_url,
                current_headers,
            )
            if response.status_code == HTTPStatus.NOT_MODIFIED:
                verify_https_enforcement(response, url)
                return response

            if not response.is_redirect:
                verify_https_enforcement(response, url)
                response.raise_for_status()
                return response

            next_url = response.headers.get("Location")
            if not next_url:
                response.raise_for_status()
                return response

            next_url = str(response.url.join(next_url))
            if not await self._is_safe_url(next_url):
                _LOGGER.warning("Blocking redirect to unsafe URL: %s", redact_url(next_url))
                raise BlueprintFetchPolicyError(
                    f"Security violation: Redirected to unsafe URL {redact_url(next_url)}"
                )

            current_url = next_url
            current_headers = {
                k: v
                for k, v in current_headers.items()
                if k.lower() in ("if-none-match", "if-modified-since")
            }

        _LOGGER.error("Too many redirects (%d) fetching %s", max_redirects, redact_url(url))
        raise BlueprintFetchPolicyError("Too many redirects")

    @staticmethod
    async def _async_get_bounded_response(
        session: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Stream one identity-encoded response and enforce the decoded byte ceiling.

        Blueprint downloads intentionally disable content coding. Caller headers are copied,
        but any ``Accept-Encoding`` value is overridden to keep that policy centralized.
        """
        request_headers = httpx.Headers(headers)
        request_headers["Accept-Encoding"] = "identity"
        request = session.build_request(
            "GET",
            url,
            headers=request_headers,
            timeout=REQUEST_TIMEOUT,
        )
        response = await session.send(request, stream=True, follow_redirects=False)
        try:
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    parsed_length = int(declared_length)
                except ValueError:
                    parsed_length = -1
                if parsed_length > MAX_RESPONSE_BYTES:
                    raise BlueprintFetchPolicyError(
                        f"Blueprint response exceeds {MAX_RESPONSE_BYTES} byte limit"
                    )

            body = bytearray()
            body_iterator = response.aiter_bytes()
            try:
                async for chunk in body_iterator:
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise BlueprintFetchPolicyError(
                            f"Blueprint response exceeds {MAX_RESPONSE_BYTES} byte limit"
                        )
            finally:
                if close_iterator := getattr(body_iterator, "aclose", None):
                    await close_iterator()

            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    @staticmethod
    def _decode_response_text(response: httpx.Response, url: str) -> str:
        """Decode one response as strict UTF-8, accepting an optional BOM."""
        content = response.content
        if not isinstance(content, bytes):
            text = response.text
            if isinstance(text, str):
                return text.removeprefix("\ufeff")
            raise BlueprintFetchPolicyError(
                f"Blueprint response from {redact_url(url)} did not contain bytes"
            )
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError as err:
            raise BlueprintFetchPolicyError(
                f"Blueprint response from {redact_url(url)} is not valid UTF-8"
            ) from err

    @staticmethod
    async def _parse_provider_response(
        response: httpx.Response,
        url: str,
    ) -> str | None:
        """Extract blueprint content from the HTTP response using the matching provider.

        If a provider is registered for the URL, parses its content (handling
        JSON decoding for API endpoints). Falls back to raw response text for
        plain-text sources. Returns None for 304 Not Modified responses.

        Args:
            response: The httpx.Response from the server.
            url: The original URL used to locate the correct provider.

        Returns:
            Blueprint content string, or None for a 304 Not Modified response.

        """
        if response.status_code == HTTPStatus.NOT_MODIFIED:
            return None

        content_type = response.headers.get("Content-Type", "")
        normalized_ct = content_type.split(";", 1)[0].strip().lower()
        decoded_text = BlueprintUpdateCoordinator._decode_response_text(response, url)
        provider = registry.get_provider(str(response.url))
        if provider is None:
            if normalized_ct in (
                "application/yaml",
                "application/x-yaml",
                "text/yaml",
                "text/x-yaml",
                "text/plain",
            ):
                return decoded_text

            raise HomeAssistantError(
                f"Unsupported content type '{content_type}' for YAML blueprint "
                f"at URL '{redact_url(str(response.url))}'. No provider was found for this URL."
            )
        is_json = normalized_ct in ("application/json", "text/json")
        json_data = None
        if is_json:
            try:
                json_data = orjson.loads(decoded_text)
            except orjson.JSONDecodeError as err:
                raise HomeAssistantError(
                    f"Invalid JSON response from provider at {redact_url(url)} "
                    f"(Content-Type: {content_type}): {err}"
                ) from err

        content = provider.parse_content(decoded_text, json_data)
        if content is None:
            if is_json:
                raise HomeAssistantError(
                    f"Failed to extract blueprint content from JSON response at {redact_url(url)}"
                )
            raise HomeAssistantError(
                f"Failed to extract blueprint content from response at {redact_url(url)}"
            )
        return content

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

        A fast-path is used when the content is already normalized,
        avoiding unnecessary string operations.

        Args:
            content: Raw YAML content string.

        Returns:
            Normalized YAML content.

        """
        if "\r" not in content and not content.startswith("\ufeff"):
            return content

        if content.startswith("\ufeff"):
            content = content[1:]

        return content.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _hash_content(
        content: str, source_url: str | None = None, already_normalized: bool = False
    ) -> str:
        """Calculate a deterministic SHA-256 hash of normalized content.

        This method supports both plain normalization (content only) and
        semantic normalization (content + source location tracking).

        If a source_url is provided, it is injected into the blueprint's
        metadata before hashing. This ensures the identity (logic + source)
        is preserved. If source_url is None or empty, only plain YAML
        normalization is performed.

        Args:
            content: The raw YAML string to hash.
            source_url: Optional source URL to trigger identity-aware hashing.
            already_normalized: If True, bypass normalization steps and hash raw content.

        Returns:
            The SHA-256 hex digest of the normalized content.

        """
        if already_normalized:
            return hashlib.sha256(content.encode("utf-8")).hexdigest()

        if not source_url:
            return hashlib.sha256(
                BlueprintUpdateCoordinator._normalize_content(content).encode("utf-8")
            ).hexdigest()

        normalized = BlueprintUpdateCoordinator._ensure_source_url(content, source_url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _get_blueprint_schema(domain: str) -> vol.Schema | vol.All:
        """Return the appropriate Home Assistant blueprint schema for a given domain.

        Args:
            domain: The blueprint domain (automation, script, or template).

        Returns:
            The corresponding voluptuous Schema.

        """
        if domain == FunctionalDomain.AUTOMATION:
            return AUTOMATION_BLUEPRINT_SCHEMA
        if domain == FunctionalDomain.TEMPLATE:
            if isinstance(TEMPLATE_BLUEPRINT_SCHEMA, vol.Schema):
                return TEMPLATE_BLUEPRINT_SCHEMA
            return BLUEPRINT_SCHEMA
        return BLUEPRINT_SCHEMA

    @staticmethod
    def _ensure_source_url_cached(content: str, source_url: str) -> str:
        """Implementation of source URL normalization.

        Assumes content and source_url are both strings.
        """
        source_url = source_url.strip()

        try:
            parsed = yaml_util.parse_yaml(content)
        except HomeAssistantError:
            parsed = None

        if not isinstance(parsed, dict) or "blueprint" not in parsed:
            return BlueprintUpdateCoordinator._normalize_content(content)

        blueprint_info = parsed["blueprint"]
        if not isinstance(blueprint_info, dict):
            return BlueprintUpdateCoordinator._normalize_content(content)

        blueprint_info["source_url"] = source_url

        target_data = parsed
        try:
            domain = blueprint_info.get("domain", FunctionalDomain.AUTOMATION)
            schema = BlueprintUpdateCoordinator._get_blueprint_schema(domain)
            normalized = schema(parsed)
            target_data = BlueprintUpdateCoordinator._stabilize_yaml_structure(parsed, normalized)
        except (vol.Invalid, KeyError, TypeError, ValueError) as err:
            _LOGGER.debug(
                "Semantic normalization skipped for %s (falling back to canonical YAML): %s",
                redact_url(source_url),
                err,
            )

        if isinstance(target_data, (dict, list)):
            try:
                return yaml_util.dump(target_data)
            except Exception as err:
                _LOGGER.warning(
                    "YAML canonicalization failed for %s: %s",
                    redact_url(source_url),
                    err,
                )
                return BlueprintUpdateCoordinator._normalize_content(content)
        return ""

    @staticmethod
    def _ensure_source_url(content: object, source_url: object) -> str:
        """Ensure the target source_url is present in the blueprint metadata.

        Always uses structured YAML parsing to guarantee data integrity and
        consistency with Home Assistant's core blueprint handling.

        Note: This method intentionally overwrites any existing `source_url`
        in the blueprint metadata with the provided `source_url` to ensure
        the integration tracks the authoritative source.

        It also applies semantic normalization using Home Assistant's official
        schemas (AUTOMATION_BLUEPRINT_SCHEMA or BLUEPRINT_SCHEMA). This ensures
        that default values for selectors and structural expansions (like list
        normalization) are applied consistently, matching how Home Assistant
        Core saves blueprints to disk.

        Args:
            content: Raw YAML blueprint content.
            source_url: Target URL to enforce in the content.

        Returns:
            The YAML content with the target source_url guaranteed to be
            present in the blueprint block, in canonical normalized YAML form.

        """
        if not isinstance(content, str):
            _LOGGER.debug("Non-string content passed to _ensure_source_url: %s", type(content))
            return ""
        if not isinstance(source_url, str):
            _LOGGER.debug(
                "Non-string source_url passed to _ensure_source_url: %s", type(source_url)
            )
            return BlueprintUpdateCoordinator._normalize_content(content)
        return BlueprintUpdateCoordinator._ensure_source_url_cached(content, source_url)

    @staticmethod
    def _stabilize_yaml_structure(orig_data: object, normalized_data: object) -> object:
        """Recursively update normalized structures using original key ordering.

        Preserves existing dict/list identities when possible.
        """
        if isinstance(orig_data, dict) and isinstance(normalized_data, dict):
            orig_dict: dict[object, object] = dict(orig_data.items())
            norm_dict: dict[object, object] = dict(normalized_data.items())
            res: dict[object, object] = {
                k: BlueprintUpdateCoordinator._stabilize_yaml_structure(orig_val, norm_dict[k])
                for k, orig_val in orig_dict.items()
                if k in norm_dict
            }
            new_keys = sorted([k for k in norm_dict if k not in res], key=str)
            for key in new_keys:
                res[key] = BlueprintUpdateCoordinator._stabilize_yaml_structure(
                    norm_dict[key], norm_dict[key]
                )
            return res

        if isinstance(normalized_data, list):
            orig_list = orig_data if isinstance(orig_data, list) else []
            res_list: list[object] = []

            for i, item in enumerate(normalized_data):
                orig_item = orig_list[i] if i < len(orig_list) else None
                res_list.append(
                    BlueprintUpdateCoordinator._stabilize_yaml_structure(orig_item, item)
                )
            return res_list

        return normalized_data

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

        local_text = BlueprintUpdateCoordinator._ensure_source_url(local_text, source_url)
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
    def _extract_blueprint_text(content: str) -> str:
        """Extract only the blueprint block text to avoid parsing huge YAMLs."""
        lines = content.splitlines(keepends=True)
        blueprint_lines: list[str] = []
        in_blueprint = False
        for line in lines:
            if line.startswith("blueprint:"):
                in_blueprint = True
                blueprint_lines.append(line)
            elif in_blueprint:
                if line.strip() and line[0] not in (" ", "\t", "#"):
                    break
                blueprint_lines.append(line)
        return "".join(blueprint_lines) if in_blueprint else content

    @staticmethod
    def _get_blueprint_block(
        path: str,
        content: str | None = None,
        parsed_data: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Extract the blueprint block from YAML content or pre-parsed data."""
        parsed = parsed_data
        if not parsed and content:
            content_to_parse = BlueprintUpdateCoordinator._extract_blueprint_text(content)
            try:
                parsed = yaml_util.parse_yaml(content_to_parse)
            except HomeAssistantError:
                try:
                    parsed = yaml_util.parse_yaml(content)
                except HomeAssistantError as err:
                    _LOGGER.warning("Failed to parse blueprint at %s", path)
                    _LOGGER.debug("Blueprint parse error at %s: %s", path, err)
                    return None

        if not isinstance(parsed, dict):
            _LOGGER.debug(
                "Skipping blueprint at %s: parsed YAML is not a mapping (got %s)",
                path,
                type(parsed).__name__,
            )
            return None

        if "blueprint" not in parsed:
            _LOGGER.debug(
                "Skipping blueprint at %s: missing top-level 'blueprint' key",
                path,
            )
            return None

        bp_info = parsed["blueprint"]
        if not isinstance(bp_info, dict):
            _LOGGER.debug(
                "Skipping blueprint at %s: 'blueprint' key is not a mapping (got %s)",
                path,
                type(bp_info).__name__,
            )
            return None

        return {str(k): v for k, v in bp_info.items()} if isinstance(bp_info, dict) else None

    @staticmethod
    def _parse_blueprint_data(
        path: str,
        content: str,
        relative_path: str | None = None,
        file_hash: str | None = None,
    ) -> ParsedBlueprintData | None:
        """Parse raw YAML content and extract blueprint metadata if valid.

        If a relative_path is provided, the physical directory structure
        (automation, script, or template) always takes precedence over the
        domain declared in metadata. This ensures 100% parity with Home
        Assistant Core's loading behavior and the integration's background
        refresh logic.

        """
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
        domain = normalize_domain(bp_info.get("domain"))

        if relative_path:
            parts = relative_path.split("/")
            if len(parts) >= 2 and parts[0] in ALLOWED_RELOAD_DOMAINS:
                domain = parts[0]

        return {
            "name": name,
            "domain": domain,
            "source_url": source_url.strip(),
            "local_hash": BlueprintUpdateCoordinator._hash_content(content, source_url),
            "local_file_hash": (
                file_hash
                if file_hash is not None
                else hashlib.sha256(content.encode("utf-8")).hexdigest()
            ),
        }

    @staticmethod
    def _read_blueprint_file(full_path: str) -> tuple[str, str]:
        """Read a blueprint file, decode as UTF-8, and compute physical SHA-256 hash.

        Handles binary bytes (standard runtime) and str mocks (unit test fixtures).
        """
        with open(full_path, "rb") as f:
            raw_bytes = f.read()

        if isinstance(raw_bytes, bytes):
            content = raw_bytes.decode("utf-8")
            file_hash = hashlib.sha256(raw_bytes).hexdigest()
        else:
            content = str(raw_bytes)
            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return content, file_hash

    @staticmethod
    def _scan_single_blueprint_file(
        full_path: str,
        context: BlueprintScanContext,
    ) -> BlueprintMetadata | None:
        """Scan and process a single blueprint file."""
        if os.path.islink(full_path):
            _LOGGER.warning("Skipping blueprint symlink: %s", full_path)
            return None
        real_full_path = os.path.realpath(full_path)
        try:
            if (
                os.path.commonpath([real_full_path, context.real_blueprint_path])
                != context.real_blueprint_path
            ):
                _LOGGER.warning(
                    "Security alert: Ignoring blueprint symlink outside root: %s",
                    full_path,
                )
                return None
        except (ValueError, OSError):
            _LOGGER.warning("Skipping blueprint with invalid path: %s", full_path)
            return None

        relative_path = get_blueprint_relative_path(context.hass, full_path)
        if not relative_path:
            return None

        try:
            content, file_hash = BlueprintUpdateCoordinator._read_blueprint_file(full_path)

            parsed_data = BlueprintUpdateCoordinator._parse_blueprint_data(
                full_path, content, relative_path, file_hash=file_hash
            )
            if not parsed_data:
                return None

            if not should_include_blueprint(
                relative_path, context.filter_mode, context.selected_set
            ):
                return None

            backups_count = BlueprintUpdateCoordinator._count_backups_sync(
                real_full_path, context.max_backups
            )
            return {
                **parsed_data,
                "relative_path": relative_path,
                "backups_count": backups_count,
            }
        except UnicodeDecodeError as err:
            _LOGGER.warning("Skipping non-UTF-8 blueprint at %s: %s", full_path, err)
            return None
        except OSError:
            _LOGGER.exception("Error reading blueprint at %s", full_path)
            return None

    @staticmethod
    def scan_blueprints(
        hass: HomeAssistant,
        filter_mode: FilterMode,
        selected_blueprints: list[str],
        max_backups: int = DEFAULT_MAX_BACKUPS,
    ) -> dict[str, BlueprintMetadata]:
        """Scan the blueprints directory for YAML files with source_url."""
        blueprint_path: str = hass.config.path(BLUEPRINTS_DATA_DIR)
        found_blueprints: dict[str, BlueprintMetadata] = {}

        if not os.path.isdir(blueprint_path):
            _LOGGER.debug("Blueprints directory not found: %s", blueprint_path)
            return found_blueprints

        _LOGGER.debug("Scanning blueprints in: %s", blueprint_path)
        real_blueprint_path = os.path.realpath(blueprint_path)
        selected_set = set(selected_blueprints)

        context = BlueprintScanContext(
            hass=hass,
            real_blueprint_path=real_blueprint_path,
            filter_mode=filter_mode,
            selected_set=selected_set,
            max_backups=max_backups,
        )

        for domain in ALLOWED_RELOAD_DOMAINS:
            domain_path = os.path.join(blueprint_path, domain)
            if not os.path.isdir(domain_path):
                continue

            for root, _, files in os.walk(domain_path):
                for file in files:
                    if not file.endswith((".yaml", ".yml")):
                        continue

                    full_path = os.path.join(root, file)
                    if metadata := BlueprintUpdateCoordinator._scan_single_blueprint_file(
                        full_path,
                        context,
                    ):
                        found_blueprints[full_path] = metadata

        return found_blueprints

    @staticmethod
    def _execute_restore_file(
        real_path: str,
        version: int,
        max_backups: int,
        validated_content: str | None = None,
        precondition: FileRevisionPrecondition | None = None,
    ) -> tuple[bool, str, int]:
        """Atomic filesystem operation for restoration (runs in executor)."""
        try:
            content = validated_content
            if content is None:
                content = BlueprintFileStore.read_backup(real_path, version)
            result = BlueprintFileStore.restore(
                real_path,
                content,
                max_backups,
                precondition=precondition,
            )
            return True, "success", result.backups_count
        except FileNotFoundError:
            return False, "missing_backup", 0
        except FileRevisionMismatchError as err:
            _LOGGER.warning("Rejected stale blueprint restore at %s: %s", real_path, err)
            return False, _RESTORE_REVISION_MISMATCH, 0
        except (OSError, ValueError) as err:
            _LOGGER.exception("Filesystem error during blueprint restoration: %s", err)
            return False, "system_error", 0
