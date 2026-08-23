"""Repairs flows for Blueprints Updater."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    DOMAIN,
    FilterMode,
    FunctionalDomain,
    RepairAction,
    RepairError,
    RepairRiskAction,
)
from .coordinator import BlueprintUpdateCoordinator
from .file_store import BlueprintFileStore
from .utils import (
    get_blueprint_usage_entities,
    get_validated_filter_mode,
    normalize_domain,
    redact_url,
)

_LOGGER = logging.getLogger(__name__)


class WithdrawnBlueprintRepairFlow(RepairsFlow):
    """Handler for withdrawn blueprint repair flow."""

    def __init__(
        self,
        coordinator: BlueprintUpdateCoordinator,
        issue_id: str,
        data: dict[str, Any] | None,
    ) -> None:
        """Initialize the repair flow."""
        self.coordinator = coordinator
        self.issue_id = issue_id
        self.issue_data: dict[str, Any] = dict(data) if isinstance(data, (dict, Mapping)) else {}
        self.relative_path: str = str(self.issue_data.get("relative_path") or "").strip()
        self.path: str = str(self.issue_data.get("path") or "").strip()
        self.domain: FunctionalDomain = normalize_domain(self.issue_data.get("domain"))
        self.blueprint_name: str = str(
            self.issue_data.get("name") or self.relative_path or "Unknown"
        )
        self.source_url: str = str(self.issue_data.get("source_url") or "")

        self._pending_url: str | None = None
        self._pending_content: str | None = None
        self._pending_canonical_url: str | None = None
        self._detected_risks: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the initial menu step."""
        if not self.relative_path or not self.path:
            return self.async_abort(reason="missing_issue_data")

        return self.async_show_menu(
            step_id="init",
            menu_options=[
                RepairAction.CHANGE_URL,
                RepairAction.STOP_TRACKING,
                RepairAction.DELETE_BLUEPRINT,
            ],
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "source_url": redact_url(self.source_url),
            },
        )

    async def async_step_stop_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Stop tracking the blueprint by updating config entry filter options."""
        return await self._async_execute_stop_tracking()

    async def _async_execute_stop_tracking(self) -> data_entry_flow.FlowResult:
        """Update filter mode options to exclude this blueprint."""
        if config_entry := self.coordinator.config_entry:
            filter_mode = get_validated_filter_mode(
                config_entry.options.get(CONF_FILTER_MODE, FilterMode.ALL)
            )
            selected = list(config_entry.options.get(CONF_SELECTED_BLUEPRINTS, []))

            if filter_mode == FilterMode.WHITELIST:
                new_selected = [p for p in selected if p != self.relative_path]
                new_options = {**config_entry.options, CONF_SELECTED_BLUEPRINTS: new_selected}
            elif filter_mode == FilterMode.BLACKLIST:
                new_selected = list(dict.fromkeys([*selected, self.relative_path]))
                new_options = {**config_entry.options, CONF_SELECTED_BLUEPRINTS: new_selected}
            else:  # FilterMode.ALL
                new_options = {
                    **config_entry.options,
                    CONF_FILTER_MODE: FilterMode.BLACKLIST.value,
                    CONF_SELECTED_BLUEPRINTS: [self.relative_path],
                }

            self.hass.config_entries.async_update_entry(config_entry, options=new_options)

        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_change_url(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Handle URL input and validation step."""
        flow_errors: dict[str, str] = dict(errors) if errors else {}
        if user_input is not None:
            if new_url := str(user_input.get("url", "")).strip():
                try:
                    (
                        content,
                        canonical_url,
                        _author,
                        _name,
                        _resp,
                    ) = await self.coordinator.async_fetch_import_data(new_url)
                    self._pending_url = new_url
                    self._pending_content = content
                    self._pending_canonical_url = canonical_url

                    # Detect breaking risks against current local version
                    risks = await self.coordinator.async_detect_risks_for_update(
                        self.path,
                        {
                            "relative_path": self.relative_path,
                            "domain": self.domain,
                            "name": self.blueprint_name,
                        },
                        content,
                    )
                    if risks:
                        self._detected_risks = [dict(r) for r in risks]
                        return await self.async_step_confirm_risks()

                    return await self._async_apply_new_url(content, canonical_url)
                except (httpx.HTTPError, HomeAssistantError, TimeoutError, ValueError) as err:
                    _LOGGER.warning(
                        "Failed to validate new blueprint URL %s: %s",
                        redact_url(new_url),
                        err,
                    )
                    flow_errors["url"] = RepairError.INVALID_URL

            else:
                flow_errors["url"] = RepairError.MISSING_URL
        return self.async_show_form(
            step_id="change_url",
            data_schema=vol.Schema(
                {
                    vol.Required("url", default=self._pending_url or ""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    )
                }
            ),
            errors=flow_errors,
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
            },
        )

    async def async_step_confirm_risks(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle compatibility risk confirmation step."""
        if user_input is not None:
            action = user_input.get("risk_action")
            if action == RepairRiskAction.PROCEED:
                if self._pending_content and self._pending_canonical_url:
                    return await self._async_apply_new_url(
                        self._pending_content, self._pending_canonical_url
                    )
                return await self.async_step_change_url(errors={"url": RepairError.INVALID_URL})
            if action == RepairRiskAction.DIFFERENT_URL:
                return await self.async_step_change_url()
            if action == RepairRiskAction.STOP_TRACKING:
                return await self._async_execute_stop_tracking()

        risk_summary = await self.coordinator.async_summarize_risks(self._detected_risks)

        return self.async_show_form(
            step_id="confirm_risks",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "risk_action", default=RepairRiskAction.DIFFERENT_URL
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=RepairRiskAction.PROCEED.value,
                                    label="proceed",
                                ),
                                SelectOptionDict(
                                    value=RepairRiskAction.DIFFERENT_URL.value,
                                    label="different_url",
                                ),
                                SelectOptionDict(
                                    value=RepairRiskAction.STOP_TRACKING.value,
                                    label="stop_tracking",
                                ),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="repair_risk_action",
                        )
                    )
                }
            ),
            description_placeholders={
                "name": self.blueprint_name,
                "risk_report": risk_summary,
            },
        )

    async def _async_apply_new_url(
        self, content: str, canonical_url: str
    ) -> data_entry_flow.FlowResult:
        """Atomically install updated blueprint content with new URL."""
        if self.path in self.coordinator.data:
            self.coordinator.data[self.path]["source_url"] = canonical_url
        await self.coordinator.async_install_blueprint(
            self.path,
            content,
            reload_services=False,
            backup=True,
            source_url=canonical_url,
        )
        await self.coordinator.async_reconcile_reload_services({self.domain})
        await self.coordinator.async_request_refresh()
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_delete_blueprint(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle blueprint deletion step."""
        bp_id = (
            self.relative_path.split("/", 1)[-1]
            if "/" in self.relative_path
            else self.relative_path
        )
        usage_entities = get_blueprint_usage_entities(self.hass, self.domain, bp_id)
        if usage_entities is None:
            return self.async_show_form(
                step_id="delete_blueprint",
                data_schema=vol.Schema({}),
                errors={"base": RepairError.USAGE_DISCOVERY_FAILED},
                description_placeholders={
                    "name": self.blueprint_name,
                    "path": self.relative_path,
                    "usage_count": "?",
                    "entities": "?",
                },
            )
        usage_count = len(usage_entities)

        errors: dict[str, str] = {}
        if user_input is not None:
            if usage_count > 0 and not user_input.get("confirm_delete_in_use"):
                errors["confirm_delete_in_use"] = RepairError.CONFIRMATION_REQUIRED
            else:
                return await self._async_execute_delete()

        schema_dict: dict[Any, Any] = {}
        if usage_count > 0:
            schema_dict[vol.Required("confirm_delete_in_use", default=False)] = cv.boolean

        return self.async_show_form(
            step_id="delete_blueprint",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "usage_count": str(usage_count),
                "entities": ", ".join(usage_entities) if usage_entities else "None",
            },
        )

    async def _async_execute_delete(self) -> data_entry_flow.FlowResult:
        """Atomically delete blueprint file and backups, reload domains and purge entity."""
        await self.hass.async_add_executor_job(
            BlueprintFileStore.remove_blueprint_and_backups, self.path
        )
        await self.coordinator.async_reconcile_reload_services({self.domain})
        await self.coordinator.async_request_refresh()
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create a repair fix flow."""
    if not data:
        raise data_entry_flow.UnknownFlow("Missing required issue data")

    config_entry_id: str | None = data.get("config_entry_id")
    coordinator = BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, config_entry_id)

    if coordinator is None:
        if config_entry_id:
            raise data_entry_flow.UnknownFlow(
                f"No active coordinator found for config entry {config_entry_id}"
            )
        raise data_entry_flow.UnknownFlow(
            "No active coordinator found; either provide config_entry_id in issue data "
            "or ensure only a single coordinator exists"
        )

    return WithdrawnBlueprintRepairFlow(coordinator, issue_id, data)
