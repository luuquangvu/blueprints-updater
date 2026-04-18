"""Config flow for Pyscript Updater."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTO_UPDATE,
    CONF_GITHUB_TOKEN,
    CONF_MANIFEST_FILE,
    CONF_MAX_BACKUPS,
    CONF_PYSCRIPT_DIR,
    CONF_RELOAD_AFTER_UPDATE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MANIFEST_FILE,
    DEFAULT_PYSCRIPT_DIR,
    DEFAULT_RELOAD_AFTER_UPDATE,
    DOMAIN,
    MAX_BACKUPS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_BACKUPS,
    MIN_UPDATE_INTERVAL,
)
from .utils import get_max_backups, get_option, get_update_interval

_LOGGER = logging.getLogger(__name__)


def _build_schema(config: Any) -> vol.Schema:
    """Build the config/options schema with current defaults."""
    pyscript_dir = get_option(config, CONF_PYSCRIPT_DIR, DEFAULT_PYSCRIPT_DIR)
    manifest_file = get_option(config, CONF_MANIFEST_FILE, DEFAULT_MANIFEST_FILE)
    auto_update = get_option(config, CONF_AUTO_UPDATE, False)
    reload_after = get_option(config, CONF_RELOAD_AFTER_UPDATE, DEFAULT_RELOAD_AFTER_UPDATE)
    github_token = get_option(config, CONF_GITHUB_TOKEN, "")

    return vol.Schema(
        {
            vol.Required(CONF_PYSCRIPT_DIR, default=pyscript_dir): cv.string,
            vol.Required(CONF_MANIFEST_FILE, default=manifest_file): cv.string,
            vol.Required(CONF_AUTO_UPDATE, default=auto_update): cv.boolean,
            vol.Required(CONF_RELOAD_AFTER_UPDATE, default=reload_after): cv.boolean,
            vol.Required(CONF_UPDATE_INTERVAL, default=get_update_interval(config)): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_UPDATE_INTERVAL,
                    max=MAX_UPDATE_INTERVAL_HOURS,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfTime.HOURS,
                )
            ),
            vol.Required(CONF_MAX_BACKUPS, default=get_max_backups(config)): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_BACKUPS,
                    max=MAX_BACKUPS,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(CONF_GITHUB_TOKEN, default=github_token): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


class PyscriptUpdaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pyscript Updater."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Pyscript Updater",
                data={},
                options=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(None),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PyscriptUpdaterOptionsFlowHandler:
        """Return the options flow handler."""
        return PyscriptUpdaterOptionsFlowHandler()


class PyscriptUpdaterOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Pyscript Updater."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(self.config_entry),
        )
