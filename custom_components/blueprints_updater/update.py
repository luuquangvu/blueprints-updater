from __future__ import annotations

import logging
from typing import Any

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

from .const import CONF_AUTO_UPDATE, DOMAIN
from .coordinator import BlueprintUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Blueprints Updater update entities.

    Args:
        `hass`: HomeAssistant instance.
        `entry`: Config entry.
        `async_add_entities`: Callback to add entities.
    """
    coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    current_entities: dict[str, BlueprintUpdateEntity] = {}

    @callback
    def async_update_entities() -> None:
        """Add new blueprint entities or remove deleted ones from Home Assistant."""
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
        for path in current_entities:
            if path not in coordinator.data:
                removed_paths.append(path)

        entity_registry = er.async_get(hass)

        if removed_paths:
            for path in removed_paths:
                _LOGGER.debug("Removing blueprint update entity for deleted file: %s", path)
                entity = current_entities.pop(path)
                if entity.entity_id:
                    if entity_registry.async_get(entity.entity_id):
                        entity_registry.async_remove(entity.entity_id)
                    hass.states.async_remove(entity.entity_id)
                else:
                    hass.async_create_task(entity.async_remove(force_remove=True))

        valid_unique_ids = {
            BlueprintUpdateCoordinator.generate_unique_id(info["rel_path"])
            for info in coordinator.data.values()
        }

        entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        for entity_entry in entries:
            if entity_entry.domain == "update" and entity_entry.unique_id not in valid_unique_ids:
                _LOGGER.debug(
                    "Removing orphaned registry entry for entity: %s", entity_entry.entity_id
                )
                entity_registry.async_remove(entity_entry.entity_id)
                hass.states.async_remove(entity_entry.entity_id)

    async_update_entities()

    entry.async_on_unload(coordinator.async_add_listener(async_update_entities))


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
            `coordinator`: Update coordinator.
            `path`: Path to the blueprint.
            `info`: Blueprint metadata dict.
        """
        super().__init__(coordinator)
        self._path = path
        self._attr_name = info["name"]
        self._attr_unique_id = BlueprintUpdateCoordinator.generate_unique_id(info["rel_path"])
        self._attr_title = info["name"]
        self._attr_release_url = info.get("source_url")
        self._attr_release_summary = None
        self._localized_error: str | None = None

    @property
    def auto_update(self) -> bool:
        """Return True if auto update is enabled for this entity.

        Returns:
            Boolean indicating auto-update preference from config options.
        """
        if not self.coordinator.config_entry:
            return False
        return self.coordinator.config_entry.options.get(CONF_AUTO_UPDATE, False)

    @property
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
        notes += "\n\n" + await self.coordinator.async_translate("auto_update_warning")

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

        return notes

    @property
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes like last_error.

        Returns:
            A dictionary containing entity-specific attributes.
        """
        attrs = {}
        if self._path in self.coordinator.data:
            info = self.coordinator.data[self._path]
            if error := info.get("last_error"):
                attrs["last_error"] = self._localized_error or error
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator by localizing strings.

        Triggered whenever the coordinator finishes a refresh.
        """
        if self.hass:
            self.hass.async_create_task(self._async_localize_strings())
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

        if self.hass and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        await self._async_localize_strings()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the update.

        Args:
            `version`: The desired version to install (unused).
            `backup`: Whether a backup should be created (passed to coordinator).
        """
        if self._path not in self.coordinator.data:
            _LOGGER.error("Blueprint path %s not found in coordinator data", self._path)
            return

        info = self.coordinator.data[self._path]
        if error := info.get("last_error"):
            if "|" in error:
                key, val = error.split("|", 1)
                msg = await self.coordinator.async_translate(key, errors=val, error=val)
            else:
                msg = await self.coordinator.async_translate(error)

            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error=msg)
            )

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

        if remote_content is None:
            _LOGGER.error("Failed to install blueprint: content is missing for %s", self._path)
            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error="content_missing")
            )

        await self.coordinator.async_install_blueprint(
            self._path, remote_content, reload_services=True, backup=backup
        )
        await self.coordinator.async_refresh()
