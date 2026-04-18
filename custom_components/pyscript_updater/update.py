"""Update entities for Pyscript Updater."""

from __future__ import annotations

import contextlib
import inspect
import logging
from functools import cached_property
from typing import Any, ClassVar

import httpx
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
from .coordinator import PyscriptUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entities from a config entry."""
    coordinator: PyscriptUpdateCoordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    current: dict[str, PyscriptUpdateEntity] = {}

    @callback
    def async_sync_entities() -> None:
        """Add new entities or remove ones that no longer exist."""
        entity_registry = er.async_get(hass)

        wanted_ids = {
            PyscriptUpdateCoordinator.generate_unique_id(entry.entry_id, info["rel_path"])
            for info in coordinator.data.values()
        }

        for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
            if entity_entry.domain != "update":
                continue
            if entity_entry.unique_id in wanted_ids:
                continue
            _LOGGER.debug("Removing orphaned pyscript entity: %s", entity_entry.entity_id)
            entity_registry.async_remove(entity_entry.entity_id)
            hass.states.async_remove(entity_entry.entity_id)

        new_entities: list[PyscriptUpdateEntity] = []
        for rel_path, info in coordinator.data.items():
            if rel_path in current:
                continue
            entity = PyscriptUpdateEntity(coordinator, rel_path, info)
            current[rel_path] = entity
            new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

        for rel_path in list(current):
            if rel_path not in coordinator.data:
                entity = current.pop(rel_path)
                if entity.entity_id and entity_registry.async_get(entity.entity_id):
                    entity_registry.async_remove(entity.entity_id)
                    hass.states.async_remove(entity.entity_id)
                else:
                    hass.async_create_task(entity.async_remove(force_remove=True))

    async_sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_sync_entities))


class PyscriptUpdateEntity(CoordinatorEntity[PyscriptUpdateCoordinator], UpdateEntity):
    """Representation of a pyscript file update entity."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.BACKUP
    _attr_translation_key = "pyscript_file"

    def __init__(
        self,
        coordinator: PyscriptUpdateCoordinator,
        rel_path: str,
        info: dict[str, Any],
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._rel_path = rel_path
        self._attr_name = rel_path
        self._attr_title = rel_path
        self._attr_unique_id = PyscriptUpdateCoordinator.generate_unique_id(
            coordinator.config_entry.entry_id, rel_path
        )
        self._attr_release_url = info.get("source_url")
        self._localized_error: str | None = None

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return True when the coordinator has data."""
        return super().available

    @cached_property
    def auto_update(self) -> bool:
        """Return whether auto-update is enabled for this entry."""
        if not self.coordinator.config_entry:
            return False
        return self.coordinator.config_entry.options.get(CONF_AUTO_UPDATE, False)

    @cached_property
    def installed_version(self) -> str | None:
        """Return the short hash of the locally installed content."""
        info = self.coordinator.data.get(self._rel_path)
        if not info:
            return None
        local = info.get("local_hash") or ""
        return local[:8] if local else None

    @cached_property
    def latest_version(self) -> str | None:
        """Return the short hash of the remote content."""
        info = self.coordinator.data.get(self._rel_path)
        if not info:
            return None
        if info.get("updatable") and info.get("remote_hash"):
            return info["remote_hash"][:8]
        local = info.get("local_hash") or ""
        return local[:8] if local else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return extra attributes: source url, last error."""
        info = self.coordinator.data.get(self._rel_path, {})
        attrs: dict[str, Any] = {
            "source_url": info.get("source_url"),
            "rel_path": self._rel_path,
        }
        if error := info.get("last_error"):
            attrs["last_error"] = self._localized_error or error
        return attrs

    _cached_property_names_by_class: ClassVar[dict[type, list[str]]] = {}

    @callback
    def _clear_cached_properties(self) -> None:
        """Invalidate cached_property values after a coordinator update."""
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
        """Refresh cached state whenever the coordinator finishes a poll."""
        self._clear_cached_properties()
        if self.hass:
            self.hass.async_create_task(self._async_localize_strings())
        super()._handle_coordinator_update()

    async def _async_localize_strings(self) -> None:
        """Translate release summary + stored error message."""
        info = self.coordinator.data.get(self._rel_path)
        if not info:
            return

        if info.get("updatable"):
            self._attr_release_summary = await self.coordinator.async_translate(
                "update_available_short"
            )
        else:
            self._attr_release_summary = await self.coordinator.async_translate("up_to_date")

        self._localized_error = None
        if error := info.get("last_error"):
            if "|" in error:
                key, val = error.split("|", 1)
                self._localized_error = await self.coordinator.async_translate(key, error=val)
            else:
                self._localized_error = await self.coordinator.async_translate(error)

        if self.hass and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run first localization on add."""
        await super().async_added_to_hass()
        await self._async_localize_strings()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the latest remote content for this file."""
        info = self.coordinator.data.get(self._rel_path)
        if not info:
            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error="missing_data")
            )

        content = info.get("remote_content")
        if content is None and info.get("updatable"):
            try:
                content_bytes, etag, _ = await self.coordinator.async_fetch_bytes(info["raw_url"])
            except httpx.HTTPError as err:
                raise HomeAssistantError(
                    await self.coordinator.async_translate("install_error", error=str(err))
                ) from err
            if content_bytes is None:
                # remote did not change; nothing to install
                await self.coordinator.async_request_refresh()
                return
            content = content_bytes
            info["remote_content"] = content
            info["etag"] = etag

        if content is None:
            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error="content_missing")
            )

        try:
            await self.coordinator.async_install_file(
                self._rel_path, content, reload_after=True, backup=backup
            )
        except OSError as err:
            raise HomeAssistantError(
                await self.coordinator.async_translate("install_error", error=str(err))
            ) from err

        await self.coordinator.async_refresh()
