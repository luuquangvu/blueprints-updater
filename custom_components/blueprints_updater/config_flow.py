from __future__ import annotations

import logging
import os
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
    DEFAULT_MAX_BACKUPS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    FILTER_MODE_ALL,
    FILTER_MODE_BLACKLIST,
    FILTER_MODE_WHITELIST,
)
from .coordinator import BlueprintUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def _async_get_blueprint_options(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Scan blueprints and return options for the selector.

    Args:
        `hass`: HomeAssistant instance.

    Returns:
        List of blueprint options with value and label.
    """
    blueprints = await hass.async_add_executor_job(
        BlueprintUpdateCoordinator.scan_blueprints, hass, FILTER_MODE_ALL, []
    )
    options = [
        {
            "value": (
                rel_path := os.path.relpath(path, hass.config.path("blueprints")).replace("\\", "/")
            ),
            "label": f"{info['name']} [{rel_path}]",
        }
        for path, info in blueprints.items()
    ]
    options.sort(key=lambda x: x["label"])
    return options


def _get_config_schema(
    defaults: dict[str, Any],
    blueprint_options: list[dict[str, Any]],
) -> vol.Schema:
    """Return the configuration schema for the flow.

    Args:
        `defaults`: Current or default configuration values.
        `blueprint_options`: Available blueprints to select from.

    Returns:
        A voluptuous Schema object.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_AUTO_UPDATE,
                default=defaults.get(CONF_AUTO_UPDATE, False),
            ): cv.boolean,
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=max(1, defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS)),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfTime.HOURS,
                )
            ),
            vol.Required(
                CONF_MAX_BACKUPS,
                default=max(1, min(10, defaults.get(CONF_MAX_BACKUPS, DEFAULT_MAX_BACKUPS))),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=10,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_FILTER_MODE,
                default=defaults.get(CONF_FILTER_MODE, FILTER_MODE_ALL),
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
                default=defaults.get(CONF_SELECTED_BLUEPRINTS, []),
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

        defaults = dict(self.config_entry.options)

        return self.async_show_form(
            step_id="init",
            data_schema=_get_config_schema(defaults, options),
        )
