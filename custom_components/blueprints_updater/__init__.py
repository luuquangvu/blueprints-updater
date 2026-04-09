"""Blueprints Updater integration for Home Assistant."""

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation
from homeassistant.helpers.entity_registry import EntityRegistry
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import BlueprintUpdateCoordinator
from .utils import get_max_backups, get_update_interval

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up the Blueprints Updater component.

    Args:
        hass: HomeAssistant instance.
        _: Unused config object.

    Returns:
        True if initialization was successful.

    """

    def _clear_cache(_: Event) -> None:
        """Clear translation cache on config update."""
        if DOMAIN not in hass.data:
            return

        if "translation_cache" in hass.data[DOMAIN]:
            _LOGGER.debug("Clearing Blueprints Updater translation cache due to config change")
            hass.data[DOMAIN]["translation_cache"] = {}

        for coordinator in hass.data[DOMAIN].get("coordinators", {}).values():
            if hasattr(coordinator, "clear_translations"):
                coordinator.clear_translations()

    hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _clear_cache)

    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blueprints Updater from a config entry.

    Args:
        hass: HomeAssistant instance.
        entry: Configuration entry from the user.

    Returns:
        True if the entry was set up successfully.

    """
    _LOGGER.debug("Setting up Blueprints Updater entry: %s", entry.entry_id)

    if entry.data:
        _LOGGER.info("Migrating configuration data to options for %s", entry.entry_id)
        hass.config_entries.async_update_entry(
            entry, data={}, options={**entry.options, **entry.data}
        )

    interval_hours = get_update_interval(entry)

    blueprint_coordinator = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=interval_hours),
    )
    await blueprint_coordinator.async_setup()
    await blueprint_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {}).setdefault("coordinators", {})[entry.entry_id] = (
        blueprint_coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))
    blueprint_coordinator.setup_complete = True
    blueprint_coordinator.async_set_updated_data(blueprint_coordinator.data)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register custom services for the integration.

    Args:
        hass: HomeAssistant instance.

    """

    def _get_coordinators() -> list[BlueprintUpdateCoordinator]:
        """Get all available coordinators from hass data.

        Returns:
            List of BlueprintUpdateCoordinator instances.

        """
        return list(hass.data.get(DOMAIN, {}).get("coordinators", {}).values())

    async def _translate(key: str, category: str = "exceptions", **kwargs: str) -> str:
        """Translate a key using the coordinator if available, otherwise fallback.

        Args:
            key: Translation key.
            category: Translation category (e.g. 'exceptions', 'common').
            **kwargs: Placeholder values.

        Returns:
            Translated string.

        """
        coordinators = _get_coordinators()
        if coordinators:
            return await coordinators[0].async_translate(key, category=category, **kwargs)

        lang = getattr(hass.config, "language", "en")
        cache_key = (lang, category)
        cache = hass.data.setdefault(DOMAIN, {}).setdefault("translation_cache", {})

        if cache_key not in cache:
            try:
                cache[cache_key] = await translation.async_get_translations(
                    hass, lang, category, [DOMAIN]
                )
            except (OSError, ValueError) as err:
                _LOGGER.debug(
                    "Could not load translations for %s %s during setup: %s",
                    DOMAIN,
                    category,
                    err,
                )
                cache[cache_key] = {}

        translations = cache[cache_key]

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
        for active_coordinator in _get_coordinators():
            await active_coordinator.async_request_refresh()

    async_register_admin_service(hass, DOMAIN, "reload", async_reload_action_handler)

    async def async_restore_blueprint_handler(call: ServiceCall) -> dict:
        """Handle the restore blueprint action."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="missing_entity_id",
            )

        entity_registry: EntityRegistry = er.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry or entity_entry.domain != "update":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_entity",
            )

        config_entry_id = entity_entry.config_entry_id
        active_coordinator = (
            hass.data.get(DOMAIN, {}).get("coordinators", {}).get(config_entry_id)
            if config_entry_id
            else None
        )

        if not active_coordinator:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_found",
            )

        target_path = None
        for path, info in active_coordinator.data.items():
            expected_id = BlueprintUpdateCoordinator.generate_unique_id(
                active_coordinator.config_entry.entry_id, info["rel_path"]
            )
            legacy_id = BlueprintUpdateCoordinator.generate_legacy_unique_id(info["rel_path"])
            if entity_entry.unique_id in (expected_id, legacy_id):
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

        max_backups = get_max_backups(active_coordinator.config_entry)
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
        coordinators = _get_coordinators()
        if not coordinators:
            return

        backup_pref = call.data.get("backup", True)

        for active_coordinator in coordinators:
            try:
                targets = [
                    (path, info)
                    for path, info in active_coordinator.data.items()
                    if info.get("updatable") and not info.get("last_error")
                ]

                if not targets:
                    continue

                config_entry = active_coordinator.config_entry
                if not config_entry:
                    continue

                _LOGGER.info(
                    "Starting bulk update for up to %d blueprints in %s",
                    len(targets),
                    config_entry.entry_id,
                )

                processed_count = 0
                for path, info in targets:
                    try:
                        remote_content = info.get("remote_content")

                        if remote_content is None:
                            _LOGGER.debug("Fetching missing content for bulk update of %s", path)
                            await active_coordinator.async_fetch_blueprint(path, force=True)
                            info = active_coordinator.data.get(path, info)
                            remote_content = info.get("remote_content")

                        if remote_content:
                            await active_coordinator.async_install_blueprint(
                                path, remote_content, reload_services=False, backup=backup_pref
                            )
                            processed_count += 1
                    except Exception as err:
                        _LOGGER.exception(
                            "Failed to update blueprint path %s: %s",
                            path,
                            err,
                        )
                        continue

                if processed_count > 0:
                    await active_coordinator.async_reload_services()
                    await active_coordinator.async_request_refresh()
            except Exception:
                config_entry = getattr(active_coordinator, "config_entry", None)
                entry_id = (
                    getattr(config_entry, "entry_id", "unknown") if config_entry else "unknown"
                )
                _LOGGER.exception(
                    "Failed to update blueprints for config entry %s",
                    entry_id,
                )

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
    """Update options for a config entry.

    Args:
        hass: HomeAssistant instance.
        entry: Configuration entry.

    """
    _LOGGER.debug("Updating options for Blueprints Updater entry: %s", entry.entry_id)
    blueprint_coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN]["coordinators"][
        entry.entry_id
    ]
    interval_hours = get_update_interval(entry)
    blueprint_coordinator.config_entry = entry
    blueprint_coordinator.update_interval = timedelta(hours=interval_hours)

    await blueprint_coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: HomeAssistant instance.
        entry: Configuration entry to unload.

    Returns:
        True if the entry was unloaded successfully.

    """
    blueprint_coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN]["coordinators"][
        entry.entry_id
    ]
    await blueprint_coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].get("coordinators", {}).pop(entry.entry_id, None)

        if not hass.data[DOMAIN].get("coordinators"):
            hass.data[DOMAIN].pop("translation_cache", None)

        if not any(hass.data[DOMAIN].values()):
            for service in ["reload", "restore_blueprint", "update_all"]:
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
