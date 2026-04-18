"""Pyscript Updater integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EntityRegistry
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import PyscriptUpdateCoordinator
from .utils import get_max_backups, get_update_interval

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up the Pyscript Updater component."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pyscript Updater from a config entry."""
    _LOGGER.debug("Setting up Pyscript Updater entry: %s", entry.entry_id)

    if entry.data:
        _LOGGER.info("Migrating data to options for %s", entry.entry_id)
        hass.config_entries.async_update_entry(
            entry, data={}, options={**entry.options, **entry.data}
        )

    coordinator = PyscriptUpdateCoordinator(
        hass, entry, timedelta(hours=get_update_interval(entry))
    )
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {}).setdefault("coordinators", {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))
    coordinator.setup_complete = True
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register admin services."""

    def _get_coordinators() -> list[PyscriptUpdateCoordinator]:
        return list(hass.data.get(DOMAIN, {}).get("coordinators", {}).values())

    async def async_reload_handler(_: ServiceCall) -> None:
        """Trigger immediate refresh across all coordinators."""
        for coord in _get_coordinators():
            await coord.async_request_refresh()

    async_register_admin_service(hass, DOMAIN, "reload", async_reload_handler)

    async def async_update_all_handler(call: ServiceCall) -> None:
        """Install all available updates."""
        backup = call.data.get("backup", True)
        for coord in _get_coordinators():
            targets = [
                (rel, info)
                for rel, info in coord.data.items()
                if info.get("updatable") and not info.get("last_error")
            ]
            if not targets:
                continue

            processed = 0
            for rel, info in targets:
                content = info.get("remote_content")
                if content is None:
                    try:
                        content_bytes, etag, _ = await coord.async_fetch_bytes(info["raw_url"])
                    except httpx.HTTPError as err:
                        _LOGGER.error("Fetch failed for %s: %s", rel, err)
                        continue
                    if content_bytes is None:
                        continue
                    content = content_bytes
                    info["remote_content"] = content
                    info["etag"] = etag

                try:
                    await coord.async_install_file(rel, content, reload_after=False, backup=backup)
                    processed += 1
                except OSError as err:
                    _LOGGER.error("Install failed for %s: %s", rel, err)

            if processed > 0:
                await coord.async_reload_pyscript()
                await coord.async_request_refresh()

    async_register_admin_service(
        hass,
        DOMAIN,
        "update_all",
        async_update_all_handler,
        schema=vol.Schema({vol.Optional("backup", default=True): cv.boolean}),
    )

    async def async_restore_handler(call: ServiceCall) -> dict:
        """Restore a pyscript file from a backup."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="missing_entity_id"
            )

        entity_registry: EntityRegistry = er.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry or entity_entry.domain != "update":
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_entity"
            )

        coord = (
            hass.data.get(DOMAIN, {}).get("coordinators", {}).get(entity_entry.config_entry_id)
            if entity_entry.config_entry_id
            else None
        )
        if not coord:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_found")

        target_rel: str | None = None
        for rel_path, info in coord.data.items():
            expected = PyscriptUpdateCoordinator.generate_unique_id(
                coord.config_entry.entry_id, info["rel_path"]
            )
            if entity_entry.unique_id == expected:
                target_rel = rel_path
                break

        if target_rel is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_found")

        version = int(call.data.get("version", 1))
        max_backups = get_max_backups(coord.config_entry)
        if version < 1 or version > max_backups:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_version"
            )

        result = await coord.async_restore_file(target_rel, version=version)
        key = result.get("translation_key", "system_error")
        result["message"] = await coord.async_translate(key, category="exceptions")
        return result

    async_register_admin_service(
        hass,
        DOMAIN,
        "restore_pyscript",
        async_restore_handler,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Optional("version", default=1): vol.All(vol.Coerce(int)),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    coord: PyscriptUpdateCoordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    coord.config_entry = entry
    coord.update_interval = timedelta(hours=get_update_interval(entry))
    await coord.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coord: PyscriptUpdateCoordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    await coord.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].get("coordinators", {}).pop(entry.entry_id, None)
        if not hass.data[DOMAIN].get("coordinators"):
            for service in ("reload", "update_all", "restore_pyscript"):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
