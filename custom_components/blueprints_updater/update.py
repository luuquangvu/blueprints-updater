"""Update entities for Blueprints Updater."""

from __future__ import annotations

import contextlib
import inspect
import logging
from functools import cached_property
from typing import Any, ClassVar

from homeassistant.components.automation import automations_with_blueprint
from homeassistant.components.script import scripts_with_blueprint
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BlueprintUpdateCoordinator, StructuredRisk

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Blueprints Updater update entities.

    Args:
        hass: HomeAssistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.

    """
    coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]

    current_entities: dict[str, BlueprintUpdateEntity] = {}

    def async_update_entities_wrapper() -> None:
        """Wrapper for async_update_entities to be used as callback."""
        async_update_entities(hass, entry, coordinator, current_entities, async_add_entities)

    async_update_entities_wrapper()

    entry.async_on_unload(coordinator.async_add_listener(async_update_entities_wrapper))


async def _async_purge_entity_registry(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    entity: BlueprintUpdateEntity | None = None,
) -> None:
    """Remove entity from registry and state machine, optionally calling async_remove.

    Args:
        hass: HomeAssistant instance.
        entity_registry: The entity registry.
        entity_id: The entity ID to remove.
        entity: Optional entity object to handle lifecycle cleanup first.
    """
    if entity and entity.hass:
        await entity.async_remove(force_remove=True)

    if entity_registry.async_get(entity_id):
        entity_registry.async_remove(entity_id)
    hass.states.async_remove(entity_id)


@callback
def async_update_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BlueprintUpdateCoordinator,
    current_entities: dict[str, BlueprintUpdateEntity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add new blueprint entities or remove deleted ones from Home Assistant."""
    entity_registry = er.async_get(hass)

    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    new_id_to_path: dict[str, str] = {}
    legacy_id_to_new_id: dict[str, str] = {}
    for info in coordinator.data.values():
        rel_path = info["rel_path"]
        new_id = BlueprintUpdateCoordinator.generate_unique_id(entry.entry_id, rel_path)
        legacy_id = BlueprintUpdateCoordinator.generate_legacy_unique_id(rel_path)
        new_id_to_path[new_id] = rel_path
        legacy_id_to_new_id[legacy_id] = new_id

    for entity_entry in entries:
        if entity_entry.domain != "update":
            continue

        unique_id = entity_entry.unique_id
        if unique_id in new_id_to_path:
            continue

        if new_id := legacy_id_to_new_id.get(unique_id):
            _LOGGER.info(
                "Migrating legacy unique_id for %s: %s -> %s",
                entity_entry.entity_id,
                unique_id,
                new_id,
            )
            entity_registry.async_update_entity(entity_entry.entity_id, new_unique_id=new_id)
            continue

        _LOGGER.debug("Removing orphaned registry entry for entity: %s", entity_entry.entity_id)
        entity_registry.async_remove(entity_entry.entity_id)
        hass.states.async_remove(entity_entry.entity_id)

    new_entities = []
    for path, info in coordinator.data.items():
        if path not in current_entities:
            entity = BlueprintUpdateEntity(coordinator, path, info)
            current_entities[path] = entity
            new_entities.append(entity)

    if new_entities:
        _LOGGER.debug("Adding %d new blueprint update entities", len(new_entities))
        async_add_entities(new_entities)

    removed_paths = []
    removed_paths.extend(path for path in current_entities if path not in coordinator.data)
    if removed_paths:
        for path in removed_paths:
            _LOGGER.debug("Removing blueprint update entity for deleted file: %s", path)
            entity = current_entities.pop(path)
            if entity.entity_id:
                hass.async_create_task(
                    _async_purge_entity_registry(hass, entity_registry, entity.entity_id, entity)
                )
            else:
                hass.async_create_task(entity.async_remove(force_remove=True))


class BlueprintUpdateEntity(CoordinatorEntity[BlueprintUpdateCoordinator], UpdateEntity):
    """Representation of a blueprint update entity."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.BACKUP | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_translation_key = "blueprint"

    def __init__(
        self,
        coordinator: BlueprintUpdateCoordinator,
        path: str,
        info: dict[str, Any],
    ) -> None:
        """Initialize the update entity.

        Args:
            coordinator: Update coordinator.
            path: Path to the blueprint.
            info: Blueprint metadata dict.
        """
        super().__init__(coordinator)
        self._path = path
        self._attr_name = info["name"]
        self._attr_unique_id = BlueprintUpdateCoordinator.generate_unique_id(
            coordinator.config_entry.entry_id, info["rel_path"]
        )
        self._attr_title = info["name"]
        self._attr_release_url = info.get("source_url")
        self._attr_release_summary = None
        self._localized_error: str | None = None
        self._localized_blocking_reason: str | None = None

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return True if entity is available.

        This override resolves a descriptor conflict between the base classes
        while maintaining CoordinatorEntity's availability logic.
        """
        return super().available

    @cached_property
    def auto_update(self) -> bool:
        """Return auto-update preference for the blueprint.

        Resolved via the coordinator helper logic (centralized config preference)
        rather than direct entity-level option reads.

        Returns:
            Boolean indicating auto-update preference.
        """
        return self.coordinator.is_auto_update_enabled()

    @cached_property
    def installed_version(self) -> str | None:
        """Version of the blueprint currently installed on the local system.

        Returns:
            The first 8 characters of the local YAML hash or None.

        """
        if self._path in self.coordinator.data:
            return self.coordinator.data[self._path]["local_hash"][:8]
        return None

    async def async_release_notes(self) -> str | None:
        """Return full release notes for the update.

        This calls the dynamic generator to ensure language-accurate notes.

        Returns:
            Release notes string or None.

        """
        return await self.async_generate_release_notes()

    async def async_generate_release_notes(self) -> str | None:
        """Generate release notes dynamically based on current language.

        Returns:
            Formatted release notes string or None if not applicable.
        """
        if self._path not in self.coordinator.data:
            return None

        info = self.coordinator.data[self._path]
        if not info["updatable"]:
            return None

        notes = await self.coordinator.async_translate(
            "update_available", source_url=info.get("source_url", "<unknown>")
        )
        rel_path = info.get("rel_path", "")
        parts = rel_path.split("/", 1)
        domain = parts[0]
        bp_id = parts[-1] if len(parts) > 1 else rel_path

        total_usage = 0
        try:
            if domain == "automation":
                total_usage = len(automations_with_blueprint(self.coordinator.hass, bp_id))
            elif domain == "script":
                total_usage = len(scripts_with_blueprint(self.coordinator.hass, bp_id))
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Error calculating %s usage for blueprint %s: %s",
                domain,
                bp_id,
                err,
                exc_info=True,
            )

        if total_usage > 0:
            notes += "\n\n" + await self.coordinator.async_translate(
                "usage_warning", count=total_usage, domain=domain
            )

        breaking_risks: list[StructuredRisk] = info.get("breaking_risks", [])
        if breaking_risks:
            risks_title = await self.coordinator.async_translate("breaking_risks_title")
            risk_summary = await self.coordinator.async_summarize_risks(breaking_risks)
            notes += f"\n\n{risks_title}\n{risk_summary}\n"

        notes += "\n\n" + await self.coordinator.async_translate("update_safety_message")

        diff_result = await self.coordinator.async_get_git_diff(self._path)

        if diff_result:
            diff_text = diff_result.diff_text
            is_semantic_sync = diff_result.is_semantic_sync

            if is_semantic_sync:
                notes += "\n\n" + await self.coordinator.async_translate("semantic_sync_notice")

            if diff_text:
                fence = "```"
                while fence in diff_text:
                    fence += "`"

                diff_title = await self.coordinator.async_translate("git_diff_title")
                notes += (
                    f"\n\n<details>\n<summary>{diff_title}</summary>\n\n"
                    f"{fence}diff\n{diff_text}\n{fence}\n</details>"
                )

        return notes

    @cached_property
    def latest_version(self) -> str | None:
        """Latest version available for install from the remote source.

        Returns:
            Remote hash string (trimmed) or local hash if up-to-date.

        """
        if self._path not in self.coordinator.data:
            return None
        data = self.coordinator.data[self._path]
        if data.get("updatable") and "remote_hash" in data:
            return data["remote_hash"][:8]
        return data["local_hash"][:8]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the extra state attributes like last_error.

        Returns:
            A dictionary containing entity-specific attributes.

        """
        attrs = {}
        if self._path in self.coordinator.data:
            info = self.coordinator.data[self._path]
            if error := info.get("last_error"):
                attrs["last_error"] = self._localized_error or error
            if blocking := info.get("update_blocking_reason"):
                attrs["update_blocking_reason"] = self._localized_blocking_reason or blocking
            if risks := info.get("breaking_risks"):
                attrs["breaking_risks"] = risks
        return attrs

    _cached_property_names_by_class: ClassVar[dict[type, list[str]]] = {}

    @callback
    def _clear_cached_properties(self) -> None:
        """Invalidate cached properties after state changes."""
        cls = self.__class__
        if cls not in self._cached_property_names_by_class:
            self._cached_property_names_by_class[cls] = [
                name
                for name, _ in inspect.getmembers(cls, lambda x: isinstance(x, cached_property))
            ]

        for name in self._cached_property_names_by_class[cls]:
            with contextlib.suppress(AttributeError):
                delattr(self, name)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator by localizing strings.

        Triggered whenever the coordinator finishes a refresh.
        """
        if self.hass:
            self.hass.async_create_task(self._async_localize_strings())

        self._clear_cached_properties()
        super()._handle_coordinator_update()

    async def _async_localize_strings(self) -> None:
        """Fetch translations and update localized strings."""
        if self._path not in self.coordinator.data:
            return

        info = self.coordinator.data[self._path]
        if not info["updatable"]:
            self._attr_release_summary = await self.coordinator.async_translate("up_to_date")
        else:
            self._attr_release_summary = await self.coordinator.async_translate(
                "update_available_short"
            )

        self._localized_error = None
        if error := info.get("last_error"):
            if "|" in error:
                key, val = error.split("|", 1)
                self._localized_error = await self.coordinator.async_translate(
                    key, errors=val, error=val
                )
            else:
                self._localized_error = await self.coordinator.async_translate(error)

        self._localized_blocking_reason = None
        if blocking := info.get("update_blocking_reason"):
            name = info.get("name") or self.name
            self._localized_blocking_reason = await self.coordinator.async_translate(
                blocking, name=name
            )

        if self.hass and self.entity_id:
            self.async_write_ha_state()

    async def _translate_and_raise_last_error(self, info: dict[str, Any]) -> None:
        """Translate the last error and raise HomeAssistantError."""
        if error := info.get("last_error"):
            if "|" in error:
                key, val = error.split("|", 1)
                msg = await self.coordinator.async_translate(key, errors=val, error=val)
            else:
                msg = await self.coordinator.async_translate(error)

            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error=msg)
            )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        await self._async_localize_strings()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the update.

        Args:
            version: The desired version to install (unused).
            backup: Whether a backup should be created (passed to coordinator).
            **kwargs: Additional arguments passed by the Home Assistant entity component.

        """
        if self._path not in self.coordinator.data:
            _LOGGER.error("Blueprint path %s not found in coordinator data", self._path)
            return

        info = self.coordinator.data[self._path]
        await self._translate_and_raise_last_error(info)

        _LOGGER.info(
            "Starting manual update for %s from %s",
            self._attr_name,
            info.get("source_url", "<unknown>"),
        )
        remote_content = info.get("remote_content")

        if remote_content is None and info.get("updatable"):
            _LOGGER.debug("Remote content missing for %s, fetching on-demand", self._path)
            await self.coordinator.async_fetch_blueprint(self._path, force=True)

            info = self.coordinator.data.get(self._path, info)
            remote_content = info.get("remote_content")

        await self._translate_and_raise_last_error(info)

        if info.get("updatable") is False and remote_content is None:
            _LOGGER.debug("Blueprint %s already updated during forced fetch", self._path)
            return

        if remote_content is None:
            _LOGGER.error("Failed to install blueprint: content is missing for %s", self._path)
            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error="content_missing")
            )

        await self.coordinator.async_install_blueprint(
            self._path, remote_content, reload_services=True, backup=backup
        )
        await self.coordinator.async_refresh()
