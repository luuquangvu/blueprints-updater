import hashlib
import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MAX_BACKUPS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
)
from .coordinator import BlueprintUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE]


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up the Blueprints Updater component."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blueprints Updater from a config entry."""
    _LOGGER.debug("Setting up Blueprints Updater entry: %s", entry.entry_id)

    if entry.data:
        _LOGGER.info("Migrating configuration data to options for %s", entry.entry_id)
        hass.config_entries.async_update_entry(
            entry, data={}, options={**entry.options, **entry.data}
        )

    interval_hours = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS)

    blueprint_coordinator = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=interval_hours),
    )
    await blueprint_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = blueprint_coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register services."""

    async def _get_coordinator() -> BlueprintUpdateCoordinator | None:
        return next(iter(hass.data.get(DOMAIN, {}).values()), None)

    async def _translate(key: str, category: str = "exceptions", **kwargs: str) -> str:
        active_coordinator = await _get_coordinator()
        if active_coordinator:
            return await active_coordinator.async_translate(key, category=category, **kwargs)

        lang = getattr(hass.config, "language", "en")
        translations: dict[str, str] = {}

        try:
            translations = await translation.async_get_translations(hass, lang, category, [DOMAIN])
        except Exception as err:
            _LOGGER.debug(
                "Could not load translations for %s %s during setup: %s",
                DOMAIN,
                category,
                err,
            )

        msg = translations.get(f"component.{DOMAIN}.{category}.{key}.message") or translations.get(
            f"component.{DOMAIN}.{category}.{key}", key
        )

        try:
            return msg.format(**kwargs) if kwargs else msg
        except (KeyError, ValueError, IndexError) as err:
            _LOGGER.debug("Error formatting translation for %s: %s", key, err)
            return msg

    async def async_reload_action_handler(_: ServiceCall) -> None:
        """Handle the reload action call."""
        active_coordinator = await _get_coordinator()
        if active_coordinator:
            await active_coordinator.async_request_refresh()

    async_register_admin_service(hass, DOMAIN, "reload", async_reload_action_handler)

    async def async_restore_blueprint_handler(call: ServiceCall) -> dict:
        """Handle the restore blueprint action."""
        active_coordinator = await _get_coordinator()
        if not active_coordinator:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_found",
            )

        entity_id = call.data.get("entity_id")
        if not entity_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="missing_entity_id",
            )

        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry or entity_entry.domain != "update":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_entity",
            )

        target_path = None
        for path, info in active_coordinator.data.items():
            expected_id = f"blueprint_{hashlib.sha256(info['rel_path'].encode()).hexdigest()}"
            if expected_id == entity_entry.unique_id:
                target_path = path
                break

        if not target_path:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_found",
            )

        version = int(call.data.get("version", 1))
        config_entry = active_coordinator.config_entry
        if not config_entry:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_error",
            )

        max_backups = config_entry.options.get(CONF_MAX_BACKUPS, DEFAULT_MAX_BACKUPS)
        if version < 1 or version > max_backups:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_version",
            )

        result = await active_coordinator.async_restore_blueprint(target_path, version=version)
        key = result.pop("translation_key", result.pop("message", "system_error"))
        kwargs = result.pop("translation_kwargs", {})
        result["message"] = await _translate(key, **kwargs)
        return result

    restore_schema = vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("version", default=1): vol.All(
                vol.Coerce(int),
                NumberSelector(
                    NumberSelectorConfig(
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            ),
        }
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "restore_blueprint",
        async_restore_blueprint_handler,
        schema=restore_schema,
        supports_response=SupportsResponse.ONLY,
    )

    async def async_update_all_handler(call: ServiceCall) -> None:
        """Handle updating all available blueprints."""
        active_coordinator = await _get_coordinator()
        if not active_coordinator:
            return

        backup_pref = call.data.get("backup", True)

        updatable_paths = [
            path
            for path, info in active_coordinator.data.items()
            if info.get("updatable") and info.get("remote_content") and not info.get("last_error")
        ]

        if not updatable_paths:
            return

        config_entry = active_coordinator.config_entry
        if not config_entry:
            return

        _LOGGER.info(
            "Starting bulk update for %d blueprints in %s",
            len(updatable_paths),
            config_entry.entry_id,
        )

        for path in updatable_paths:
            info = active_coordinator.data[path]
            remote_content = info["remote_content"]
            await active_coordinator.async_install_blueprint(
                path, remote_content, reload_services=False, backup=backup_pref
            )

        await active_coordinator.async_reload_services()
        await active_coordinator.async_request_refresh()

    async_register_admin_service(
        hass,
        DOMAIN,
        "update_all",
        async_update_all_handler,
        schema=vol.Schema(
            {
                vol.Optional("backup", default=True): cv.boolean,
            }
        ),
    )


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.debug("Updating options for Blueprints Updater entry: %s", entry.entry_id)
    blueprint_coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    interval_hours = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS)
    blueprint_coordinator.update_interval = timedelta(hours=interval_hours)

    await blueprint_coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in ["reload", "restore_blueprint", "update_all"]:
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
