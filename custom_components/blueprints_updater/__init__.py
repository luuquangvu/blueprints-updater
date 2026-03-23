import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    FILTER_MODE_ALL,
)
from .coordinator import BlueprintUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blueprints Updater from a config entry."""
    _LOGGER.debug("Setting up Blueprints Updater entry: %s", entry.entry_id)
    interval_hours = entry.options.get(
        CONF_UPDATE_INTERVAL,
        entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS),
    )
    filter_mode = entry.options.get(CONF_FILTER_MODE, FILTER_MODE_ALL)
    selected_blueprints = entry.options.get(CONF_SELECTED_BLUEPRINTS, [])

    blueprint_coordinator = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=interval_hours),
        filter_mode=filter_mode,
        selected_blueprints=selected_blueprints,
    )
    await blueprint_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = blueprint_coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def async_reload_action_handler(_: ServiceCall) -> None:
        """Handle the reload action call."""
        await blueprint_coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "reload", async_reload_action_handler)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.debug("Updating options for Blueprints Updater entry: %s", entry.entry_id)
    blueprint_coordinator: BlueprintUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    interval_hours = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS)
    blueprint_coordinator.update_interval = timedelta(hours=interval_hours)
    blueprint_coordinator.filter_mode = entry.options.get(CONF_FILTER_MODE, FILTER_MODE_ALL)
    blueprint_coordinator.selected_blueprints = entry.options.get(CONF_SELECTED_BLUEPRINTS, [])

    await blueprint_coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "reload")
    return unload_ok
