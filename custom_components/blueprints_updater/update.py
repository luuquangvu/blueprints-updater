from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
    """Set up the Blueprints Updater update entities."""
    coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    current_paths: set[str] = set()

    @callback
    def async_add_blueprint_entities() -> None:
        """Add new blueprint entities when discovered."""
        new_entities = []
        for path, info in coordinator.data.items():
            if path not in current_paths:
                new_entities.append(BlueprintUpdateEntity(coordinator, path, info))
                current_paths.add(path)

        if new_entities:
            _LOGGER.debug("Adding %d new blueprint update entities", len(new_entities))
            async_add_entities(new_entities)

    async_add_blueprint_entities()

    entry.async_on_unload(coordinator.async_add_listener(async_add_blueprint_entities))


class BlueprintUpdateEntity(CoordinatorEntity[BlueprintUpdateCoordinator], UpdateEntity):
    """Representation of a blueprint update entity."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(
        self,
        coordinator: BlueprintUpdateCoordinator,
        path: str,
        info: dict[str, Any],
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._path = path
        self._attr_name = info["name"]
        self._attr_unique_id = f"blueprint_{info['local_hash']}"
        self._attr_title = info["name"]

    @property
    def auto_update(self) -> bool:
        """Return True if auto update is enabled."""
        if not self.coordinator.config_entry:
            return False
        return self.coordinator.config_entry.options.get(CONF_AUTO_UPDATE, False)

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        if self._path in self.coordinator.data:
            return self.coordinator.data[self._path]["local_hash"][:8]
        return None

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        if self._path not in self.coordinator.data:
            return None
        data = self.coordinator.data[self._path]
        if data.get("updatable") and "remote_hash" in data:
            return data["remote_hash"][:8]
        return data["local_hash"][:8]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs = {}
        if error := self.coordinator.data.get(self._path, {}).get("last_error"):
            attrs["last_error"] = error
        return attrs

    @property
    def release_summary(self) -> str | None:
        """Summary of the release."""
        if self._path in self.coordinator.data:
            info = self.coordinator.data[self._path]
            if info["updatable"]:
                return f"Update available from {info['source_url']}"
            return "Up to date"
        return None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the update."""
        if self._path not in self.coordinator.data:
            _LOGGER.error("Blueprint path %s not found in coordinator data", self._path)
            return

        info = self.coordinator.data[self._path]
        if error := info.get("last_error"):
            raise HomeAssistantError(
                f"Cannot install blueprint: {error}. "
                "The remote file has errors and cannot be safely applied."
            )

        _LOGGER.info("Starting manual update for %s from %s", self._attr_name, info["source_url"])
        remote_content = info["remote_content"]

        await self.coordinator.async_install_blueprint(self._path, remote_content)
        await self.coordinator.async_refresh()
