"""Config flow for Blueprints Updater."""

from __future__ import annotations

import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AUTO_UPDATE,
    CONF_FILTER_MODE,
    CONF_MAX_BACKUPS,
    CONF_SELECTED_BLUEPRINTS,
    CONF_UPDATE_INTERVAL,
    CONF_USE_CDN,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_USE_CDN,
    DOMAIN,
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
    MAX_BACKUPS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_BACKUPS,
    MIN_UPDATE_INTERVAL,
)
from .coordinator import BlueprintUpdateCoordinator
from .utils import (
    get_config_bool,
    get_config_str,
    get_config_value,
    get_max_backups,
    get_relative_path,
    get_update_interval,
)

_LOGGER = logging.getLogger(__name__)


async def _async_get_blueprint_options(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Scan blueprints and return options for the selector.

    Args:
        hass: HomeAssistant instance.

    Returns:
        List of blueprint options with value and label.

    """
    blueprints = await hass.async_add_executor_job(
        BlueprintUpdateCoordinator.scan_blueprints, hass, FILTER_MODE_ALL, []
    )
    options = [
        {
            "value": (rel_path := info.get("rel_path") or get_relative_path(hass, path)),
            "label": f"{info['name']} [{rel_path}]",
        }
        for path, info in blueprints.items()
    ]
    options.sort(key=lambda x: x["label"])
    return options


def _get_config_schema(
    config: Any,
    blueprint_options: list[dict[str, Any]],
) -> vol.Schema:
    """Return the configuration schema for the flow.

    Args:
        config: Current or default configuration values (ConfigEntry, dict or None).
        blueprint_options: Available blueprints to select from.

    Returns:
        A voluptuous Schema object.

    """
    auto_update = get_config_bool(config, CONF_AUTO_UPDATE, DEFAULT_AUTO_UPDATE)
    use_cdn = get_config_bool(config, CONF_USE_CDN, DEFAULT_USE_CDN)
    filter_mode = get_config_str(config, CONF_FILTER_MODE, FILTER_MODE_ALL)
    selected_blueprints = get_config_value(config, CONF_SELECTED_BLUEPRINTS, [])

    return vol.Schema(
        {
            vol.Required(
                CONF_AUTO_UPDATE,
                default=auto_update,
            ): cv.boolean,
            vol.Required(
                CONF_USE_CDN,
                default=use_cdn,
            ): cv.boolean,
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=get_update_interval(config),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_UPDATE_INTERVAL,
                    max=MAX_UPDATE_INTERVAL_HOURS,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfTime.HOURS,
                )
            ),
            vol.Required(
                CONF_MAX_BACKUPS,
                default=get_max_backups(config),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_BACKUPS,
                    max=MAX_BACKUPS,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_FILTER_MODE,
                default=filter_mode,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=cast(
                        Any,
                        [
                            {"value": FILTER_MODE_ALL, "label": FILTER_MODE_ALL},
                            {"value": FILTER_MODE_WHITELIST, "label": FILTER_MODE_WHITELIST},
                            {"value": FILTER_MODE_BLACKLIST, "label": FILTER_MODE_BLACKLIST},
                        ],
                    ),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="filter_mode",
                )
            ),
            vol.Optional(
                CONF_SELECTED_BLUEPRINTS,
                default=selected_blueprints,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=cast(Any, blueprint_options),
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            ),
        }
    )


class BlueprintsUpdaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Blueprints Updater."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        _LOGGER.debug("User step in config flow (submitted: %s)", user_input is not None)
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Blueprints Updater",
                data={},
                options=user_input,
            )

        options = await _async_get_blueprint_options(self.hass)

        return self.async_show_form(
            step_id="user",
            data_schema=_get_config_schema({}, options),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> BlueprintsUpdaterOptionsFlowHandler:
        """Get the options flow for this handler."""
        return BlueprintsUpdaterOptionsFlowHandler()


class BlueprintsUpdaterOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Blueprints Updater."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        _LOGGER.debug("Options flow step init (submitted: %s)", user_input is not None)
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = await _async_get_blueprint_options(self.hass)

        return self.async_show_form(
            step_id="init",
            data_schema=_get_config_schema(self.config_entry, options),
        )
