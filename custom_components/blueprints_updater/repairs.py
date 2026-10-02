"""Repairs flows for Blueprints Updater."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from difflib import unified_diff

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

from .blueprint_validation import ensure_source_url, read_and_diff
from .const import (
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    DOMAIN,
    FilterMode,
    FunctionalDomain,
    IncompatibilitySeverity,
    PinReason,
    RepairAction,
    RepairError,
    RepairForkAction,
    RepairIncompatibleAction,
    RepairIssueType,
    RepairRiskAction,
)
from .coordinator import BlueprintUpdateCoordinator, StructuredRisk
from .exceptions import FileRevisionMismatchError
from .file_store import BlueprintFileStore, FileRevisionPrecondition
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
        data: Mapping[str, object] | None,
    ) -> None:
        """Initialize the repair flow."""
        self.coordinator = coordinator
        self.issue_id = issue_id
        self.issue_data: dict[str, object] = dict(data) if isinstance(data, (dict, Mapping)) else {}
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
        self._pending_diff: str | None = None
        self._is_semantic_sync: bool = False
        self._detected_risks: list[StructuredRisk] = []
        self._pending_precondition: FileRevisionPrecondition | None = None

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
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
        self, user_input: dict[str, object] | None = None
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
        user_input: dict[str, object] | None = None,
        errors: dict[str, str] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Handle URL input and validation step."""
        flow_errors: dict[str, str] = dict(errors) if errors else {}
        if user_input is not None:
            self._pending_url = None
            self._pending_content = None
            self._pending_canonical_url = None
            self._pending_diff = None
            self._is_semantic_sync = False
            self._detected_risks = []
            self._pending_precondition = None

            if new_url := str(user_input.get("url", "")).strip():
                try:
                    (
                        content,
                        _fetch_url,
                        _author,
                        _name,
                        _resp,
                    ) = await self.coordinator.async_fetch_import_data(new_url)
                    self._pending_url = new_url
                    self._pending_content = content
                    self._pending_canonical_url = new_url

                    self._pending_precondition = await self.hass.async_add_executor_job(
                        BlueprintFileStore.capture_precondition,
                        self.path,
                    )

                    # Generate git diff between existing local blueprint and new remote content
                    try:
                        self._pending_diff = await self.hass.async_add_executor_job(
                            read_and_diff,
                            self.path,
                            content,
                            new_url,
                        )
                    except (OSError, ValueError) as err:
                        _LOGGER.warning(
                            "Failed to generate diff for blueprint %s during URL change: %s",
                            self.path,
                            err,
                        )
                        self._pending_diff = None

                    local_hash = self.coordinator.data.get(self.path, {}).get("local_hash")
                    self._is_semantic_sync = (
                        self.coordinator._is_semantically_equal(
                            content, str(local_hash or ""), new_url
                        )
                        if local_hash
                        else False
                    )

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
                    self._detected_risks = list(risks) if risks else []
                    return await self.async_step_confirm_risks()
                except (
                    httpx.HTTPError,
                    HomeAssistantError,
                    ValueError,
                    OSError,
                ) as err:
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
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle compatibility risk confirmation step."""
        if user_input is not None:
            action = user_input.get("risk_action")
            if action == RepairRiskAction.PROCEED:
                if (
                    self._pending_content
                    and self._pending_canonical_url
                    and self._pending_precondition is not None
                ):
                    return await self._async_apply_new_url(
                        self._pending_content, self._pending_canonical_url
                    )
                return await self.async_step_change_url(errors={"url": RepairError.INVALID_URL})
            if action == RepairRiskAction.DIFFERENT_URL:
                return await self.async_step_change_url()
            if action == RepairRiskAction.STOP_TRACKING:
                return await self._async_execute_stop_tracking()

        preview_report = await self.coordinator.async_format_blueprint_notes(
            path=self.path,
            domain=self.domain,
            source_url=self._pending_canonical_url or self._pending_url or self.source_url,
            relative_path=self.relative_path,
            breaking_risks=self._detected_risks,
            diff_text=self._pending_diff,
            is_semantic_sync=self._is_semantic_sync,
            include_header=False,
            include_safety_message=False,
        )

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
                "risk_report": preview_report,
            },
        )

    async def _async_apply_new_url(
        self, content: str, canonical_url: str
    ) -> data_entry_flow.FlowResult:
        """Atomically install updated blueprint content with new URL."""
        if self._pending_precondition is None:
            return await self.async_step_change_url(errors={"url": RepairError.INVALID_URL})
        await self.coordinator.async_install_blueprint(
            self.path,
            content,
            reload_services=False,
            backup=True,
            source_url=canonical_url,
            file_precondition=self._pending_precondition,
        )
        if self.path in self.coordinator.data:
            self.coordinator.data[self.path]["source_url"] = canonical_url
        await self.coordinator.async_reconcile_reload_services({self.domain})
        await self.coordinator.async_request_refresh()
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_delete_blueprint(
        self, user_input: dict[str, object] | None = None
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
                    "take_control_tip": "",
                },
            )
        usage_count = len(usage_entities)

        errors: dict[str, str] = {}
        if user_input is not None:
            if usage_count > 0 and not user_input.get("confirm_delete_in_use"):
                errors["confirm_delete_in_use"] = RepairError.CONFIRMATION_REQUIRED
            else:
                return await self._async_execute_delete()

        schema_dict: dict[vol.Required, object] = {}
        if usage_count > 0:
            schema_dict[vol.Required("confirm_delete_in_use", default=False)] = cv.boolean

        take_control_tip = ""
        if usage_count > 0:
            tip_msg = await self.coordinator.async_translate("take_control_tip")
            take_control_tip = f"\n\n{tip_msg}"

        return self.async_show_form(
            step_id="delete_blueprint",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "usage_count": str(usage_count),
                "entities": ", ".join(usage_entities) if usage_entities else "None",
                "take_control_tip": take_control_tip,
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


class IncompatibleBlueprintRepairFlow(RepairsFlow):
    """Handler for incompatible blueprint repair flow."""

    def __init__(
        self,
        coordinator: BlueprintUpdateCoordinator,
        issue_id: str,
        data: Mapping[str, object] | None,
    ) -> None:
        """Initialize the repair flow.

        Args:
            coordinator: Coordinator instance.
            issue_id: Repair issue identifier.
            data: Issue payload data.

        """
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.issue_id = issue_id
        self.issue_data: dict[str, object] = dict(data) if isinstance(data, (dict, Mapping)) else {}
        self.relative_path: str = str(self.issue_data.get("relative_path") or "").strip()
        self.path: str = str(self.issue_data.get("path") or "").strip()
        self.domain: FunctionalDomain = normalize_domain(self.issue_data.get("domain"))
        self.blueprint_name: str = str(
            self.issue_data.get("name") or self.relative_path or "Unknown"
        )
        self.source_url: str = str(self.issue_data.get("source_url") or "")
        self.has_auto_fix: bool = str(self.issue_data.get("has_auto_fix")).lower() in {
            "true",
            "1",
        } or bool(self.issue_data.get("candidate_content"))
        self.candidate_content: str = str(self.issue_data.get("candidate_content") or "")
        self.diff_text: str = str(self.issue_data.get("diff_text") or "")
        self.breaks_in_ha_version: str = str(self.issue_data.get("breaks_in_ha_version") or "")
        self.learn_more_url: str = str(self.issue_data.get("learn_more_url") or "")
        self.author_report_url: str = str(self.issue_data.get("author_report_url") or "")
        self.ha_docs_url: str = str(self.issue_data.get("ha_docs_url") or "")
        self.severity: str = str(self.issue_data.get("severity") or "")

        self.candidate_source_file_hash: str | None = (
            str(self.issue_data["candidate_source_file_hash"])
            if self.issue_data.get("candidate_source_file_hash")
            else None
        )

        self._pending_url: str | None = None
        self._pending_content: str | None = None
        self._pending_diff: str | None = None
        self._pending_precondition: FileRevisionPrecondition | None = None
        self._parity_deprecation_summary: str | None = None
        self._fork_breaking_error: str | None = None
        self._fork_candidate: tuple[str, str] | None = None

    async def _async_capture_precondition(self) -> None:
        """Capture precondition snapshot for the current blueprint path safely."""
        if not self.path:
            self._pending_precondition = None
            return
        try:
            precondition = await self.hass.async_add_executor_job(
                BlueprintFileStore.capture_precondition,
                self.path,
            )
            if precondition is not None and precondition.must_exist and precondition.content_hash:
                self._pending_precondition = precondition
            else:
                self._pending_precondition = None
        except Exception:
            self._pending_precondition = None

    def _delete_current_issue(self) -> None:
        """Remove the active repair issue from the Home Assistant issue registry."""
        ir.async_delete_issue(self.coordinator.hass, DOMAIN, self.issue_id)

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the initial menu step.

        Args:
            user_input: Optional user input dictionary.

        Returns:
            FlowResult displaying options or menu.

        """
        if not self.relative_path or not self.path:
            self._delete_current_issue()
            return self.async_abort(reason="missing_issue_data")

        menu_options: list[str] = []
        if self.has_auto_fix and self.candidate_content:
            menu_options.append(RepairIncompatibleAction.AUTO_FIX.value)
        if self.severity == IncompatibilitySeverity.DEPRECATION.value:
            menu_options.append(RepairIncompatibleAction.ACKNOWLEDGE.value)
        menu_options.append(RepairIncompatibleAction.CHANGE_URL.value)
        persisted = self.coordinator._persisted_metadata.get(self.relative_path, {})
        if persisted.get("pinned"):
            menu_options.append(RepairIncompatibleAction.UNPIN.value)

        links: list[str] = []
        if self.author_report_url:
            links.append(f"[Report to Blueprint Author]({self.author_report_url})")
        if self.ha_docs_url:
            links.append(f"[Official Documentation]({self.ha_docs_url})")
        resource_links = "\n".join(f"- {link}" for link in links) if links else "None"

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "ha_version": getattr(self.coordinator.hass.config, "version", "Home Assistant"),
                "error_summary": str(
                    self.issue_data.get("errors")
                    or self.issue_data.get("warnings")
                    or "Unknown issue"
                ),
                "affected_entities": str(self.issue_data.get("affected_entities") or "None"),
                "resource_links": resource_links,
                "diagnostic_snippet": (
                    f"Blueprint: {self.blueprint_name} ({self.relative_path})\n"
                    f"Severity: {self.severity}\n"
                    f"Details:\n{self.issue_data.get('errors') or self.issue_data.get('warnings')}"
                ),
            },
        )

    async def async_step_auto_fix(
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the auto-fix review and apply step.

        Args:
            user_input: Optional user input dictionary.

        Returns:
            FlowResult displaying diff form or applying changes.

        """
        if self._pending_precondition is None:
            await self._async_capture_precondition()

        if user_input is not None:
            if not user_input.get("confirm_apply"):
                return self.async_show_form(
                    step_id="auto_fix",
                    data_schema=vol.Schema({vol.Required("confirm_apply", default=False): bool}),
                    errors={"base": "confirmation_required"},
                    description_placeholders={
                        "name": self.blueprint_name,
                        "path": self.relative_path,
                        "patch_notice": "Proposed modernization patch:",
                        "diff_text": self.diff_text or "No diff available",
                    },
                )
            return await self._async_execute_auto_fix()

        return self.async_show_form(
            step_id="auto_fix",
            data_schema=vol.Schema({vol.Required("confirm_apply", default=False): bool}),
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "patch_notice": "Proposed modernization patch:",
                "diff_text": self.diff_text or "No diff available",
            },
        )

    async def _async_execute_auto_fix(self) -> data_entry_flow.FlowResult:
        """Apply candidate patch, create backup, and pin blueprint.

        Returns:
            FlowResult completing the flow or aborting on failure.

        """
        info = self.coordinator.data.get(self.path, {})
        remote_h = info.get("remote_hash") or self.coordinator._persisted_metadata.get(
            self.relative_path, {}
        ).get("remote_hash")
        if self.candidate_source_file_hash:
            precondition = FileRevisionPrecondition.existing(self.candidate_source_file_hash)
        else:
            precondition = self._pending_precondition

        old_runtime_url = self.coordinator.data.get(self.path, {}).get("source_url")
        has_persisted = self.relative_path in self.coordinator._persisted_metadata
        old_persisted_url = self.coordinator._persisted_metadata.get(self.relative_path, {}).get(
            "source_url"
        )

        if self._pending_url:
            if self.path in self.coordinator.data:
                self.coordinator.data[self.path]["source_url"] = self._pending_url
            persisted = self.coordinator._persisted_metadata.setdefault(self.relative_path, {})
            persisted["source_url"] = self._pending_url

        try:
            await self.coordinator.async_install_blueprint(
                self.path,
                self.candidate_content,
                reload_services=True,
                backup=True,
                source_url=self._pending_url,
                file_precondition=precondition,
            )
        except (FileRevisionMismatchError, HomeAssistantError) as err:
            if self._pending_url:
                if old_runtime_url is not None:
                    self.coordinator.data.setdefault(self.path, {})["source_url"] = old_runtime_url
                elif self.path in self.coordinator.data:
                    self.coordinator.data[self.path].pop("source_url", None)
                if has_persisted:
                    if old_persisted_url is not None:
                        self.coordinator._persisted_metadata[self.relative_path]["source_url"] = (
                            old_persisted_url
                        )
                    else:
                        self.coordinator._persisted_metadata[self.relative_path].pop(
                            "source_url", None
                        )
                else:
                    self.coordinator._persisted_metadata.pop(self.relative_path, None)

            if isinstance(err, FileRevisionMismatchError) or isinstance(
                getattr(err, "__cause__", None), FileRevisionMismatchError
            ):
                return self.async_abort(
                    reason="file_changed",
                    description_placeholders={"error": str(err)},
                )
            return self.async_abort(
                reason="patch_failed",
                description_placeholders={"error": str(err)},
            )
        except Exception as err:
            if self._pending_url:
                if old_runtime_url is not None:
                    self.coordinator.data.setdefault(self.path, {})["source_url"] = old_runtime_url
                elif self.path in self.coordinator.data:
                    self.coordinator.data[self.path].pop("source_url", None)
                if has_persisted:
                    if old_persisted_url is not None:
                        self.coordinator._persisted_metadata[self.relative_path]["source_url"] = (
                            old_persisted_url
                        )
                    else:
                        self.coordinator._persisted_metadata[self.relative_path].pop(
                            "source_url", None
                        )
                else:
                    self.coordinator._persisted_metadata.pop(self.relative_path, None)

            return self.async_abort(
                reason="patch_failed",
                description_placeholders={"error": str(err)},
            )

        await self.coordinator.async_pin_blueprint(
            self.relative_path,
            self.path,
            PinReason.USER_APPLIED_AUTO_FIX.value,
            str(remote_h) if remote_h is not None else None,
        )
        self._delete_current_issue()
        return self.async_create_entry(title="", data={})

    async def async_step_acknowledge(
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle acknowledging and dismissing a deprecation warning.

        Args:
            user_input: Optional user input dictionary.

        Returns:
            FlowResult showing form or completing dismissal.

        """
        if user_input is None:
            return self.async_show_form(
                step_id="acknowledge",
                data_schema=vol.Schema({vol.Required("confirm_acknowledge", default=False): bool}),
                description_placeholders={
                    "name": self.blueprint_name,
                    "path": self.relative_path,
                    "breaks_in_ha_version": self.breaks_in_ha_version or "a future release",
                },
            )
        if not user_input.get("confirm_acknowledge"):
            return self.async_show_form(
                step_id="acknowledge",
                data_schema=vol.Schema({vol.Required("confirm_acknowledge", default=False): bool}),
                errors={"base": "confirmation_required"},
                description_placeholders={
                    "name": self.blueprint_name,
                    "path": self.relative_path,
                    "breaks_in_ha_version": self.breaks_in_ha_version or "a future release",
                },
            )
        local_h = (
            self.issue_data.get("local_hash")
            or self.coordinator.data.get(self.path, {}).get("local_hash")
            or self.coordinator._persisted_metadata.get(self.relative_path, {}).get("local_hash")
        )
        dismissed_data = {
            "dismissed_at_hash": str(local_h) if local_h else "",
            "dismissed_at_ha_version": getattr(self.coordinator.hass.config, "version", ""),
            "issue_id": self.issue_id,
        }
        await self.coordinator.async_set_dismissed_warning(
            self.relative_path, self.path, dismissed_data
        )
        self._delete_current_issue()
        return self.async_create_entry(title="", data={})

    async def async_step_change_url(
        self,
        user_input: dict[str, object] | None = None,
        errors: dict[str, str] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Handle switching tracking to a community fork.

        Args:
            user_input: Optional user input dictionary.
            errors: Optional errors dictionary to pre-populate form.

        Returns:
            FlowResult displaying fork URL input form or confirm step.

        """
        errors = dict(errors) if errors else {}
        if user_input is not None:
            if url := str(user_input.get("url") or "").strip():
                fork_content = ""
                try:
                    (
                        fork_content,
                        _url,
                        _author,
                        _name,
                        _resp,
                    ) = await self.coordinator.async_fetch_import_data(url)
                except (httpx.HTTPError, HomeAssistantError, ValueError, OSError) as err:
                    _LOGGER.warning("Failed to fetch fork from %s: %s", redact_url(url), err)
                    errors["url"] = "invalid_url"

                if not errors and fork_content:
                    report = await self.coordinator.async_validate_local_blueprint_compatibility(
                        self.relative_path, self.path, fork_content
                    )
                    if report.severity == IncompatibilitySeverity.BREAKING:
                        errors["base"] = "fork_breaking_error"
                        self._fork_breaking_error = "; ".join(report.errors[:2])
                    else:
                        self._pending_url = url
                        self._pending_content = fork_content
                        local_warnings = set(str(self.issue_data.get("warnings") or "").split("\n"))
                        fork_warnings = set(report.warnings)
                        same_deprecations = {
                            w for w in (local_warnings & fork_warnings) if w.strip()
                        }
                        self._parity_deprecation_summary = (
                            "; ".join(same_deprecations) if same_deprecations else None
                        )

                        try:
                            current_content, _ = await self.coordinator.hass.async_add_executor_job(
                                BlueprintUpdateCoordinator._read_blueprint_file, self.path
                            )
                        except Exception:
                            current_content = ""

                        diff_lines = list(
                            unified_diff(
                                current_content.splitlines(keepends=True),
                                fork_content.splitlines(keepends=True),
                                fromfile=f"a/{self.relative_path}",
                                tofile=f"b/{self.relative_path}",
                            )
                        )
                        self._pending_diff = "".join(diff_lines)
                        await self._async_capture_precondition()
                        if (
                            self._pending_precondition is None
                            or not self._pending_precondition.must_exist
                            or not self._pending_precondition.content_hash
                        ):
                            errors["url"] = RepairError.INVALID_URL.value
                        else:
                            domain = (
                                normalize_domain(self.issue_data.get("domain"))
                                or FunctionalDomain.AUTOMATION
                            )
                            self._fork_candidate = (
                                await self.coordinator.async_generate_modernized_candidate(
                                    self.relative_path,
                                    self.path,
                                    fork_content,
                                    domain,
                                    dynamic_replacements=report.renamed_keys,
                                )
                            )
                            return await self.async_step_confirm_fork()

            else:
                errors["url"] = "missing_url"
        return self.async_show_form(
            step_id="change_url",
            data_schema=vol.Schema(
                {vol.Required("url"): TextSelector(TextSelectorConfig(type=TextSelectorType.URL))}
            ),
            errors=errors,
            description_placeholders={
                "name": self.blueprint_name,
                "path": self.relative_path,
                "relative_path": self.relative_path,
                "ha_version": getattr(self.coordinator.hass.config, "version", "Home Assistant"),
                "error": self._fork_breaking_error or "",
            },
        )

    async def async_step_confirm_fork(
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle confirmation of switching tracking to a community fork.

        Args:
            user_input: Optional user input dictionary.

        Returns:
            FlowResult executing fork switch or redirecting.

        """
        if self._fork_candidate is None and self._pending_content:
            domain = normalize_domain(self.issue_data.get("domain")) or FunctionalDomain.AUTOMATION
            self._fork_candidate = await self.coordinator.async_generate_modernized_candidate(
                self.relative_path,
                self.path,
                self._pending_content,
                domain,
            )

        if user_input is not None:
            action = user_input.get("fork_action")
            if action == RepairForkAction.AUTO_FIX.value:
                if self._fork_candidate:
                    candidate_content, fork_diff = self._fork_candidate
                    self.candidate_content = candidate_content
                    try:
                        current_content, _ = await self.coordinator.hass.async_add_executor_job(
                            BlueprintUpdateCoordinator._read_blueprint_file, self.path
                        )
                        diff_lines = list(
                            unified_diff(
                                current_content.splitlines(keepends=True),
                                candidate_content.splitlines(keepends=True),
                                fromfile=f"a/{self.relative_path}",
                                tofile=f"b/{self.relative_path}",
                            )
                        )
                        self.diff_text = "".join(diff_lines)
                    except Exception:
                        self.diff_text = fork_diff
                    if (
                        self._pending_precondition
                        and self._pending_precondition.must_exist
                        and self._pending_precondition.content_hash
                    ):
                        self.candidate_source_file_hash = self._pending_precondition.content_hash
                        return await self.async_step_auto_fix()
                    return await self.async_step_change_url(
                        errors={"url": RepairError.INVALID_URL.value}
                    )
                return await self._async_execute_fork_switch()
            if action == RepairForkAction.DIFFERENT_URL.value:
                return await self.async_step_change_url()
            return await self._async_execute_fork_switch()

        if self._parity_deprecation_summary:
            rem_ver = self.breaks_in_ha_version or "a future release"
            fork_notice = (
                f"**Notice: Fork Contains the Same Deprecation Warning**\n"
                f"This fork also uses deprecated syntax ({self._parity_deprecation_summary}) "
                f"scheduled for removal in Home Assistant {rem_ver}. "
                f"Switching will update tracking, but will not resolve upcoming deprecations."
            )
        else:
            fork_notice = "This fork passes Home Assistant validation with zero breaking errors."

        options = [
            SelectOptionDict(
                value=RepairForkAction.PROCEED.value,
                label=RepairForkAction.PROCEED.value,
            ),
        ]
        if self._fork_candidate is not None:
            options.append(
                SelectOptionDict(
                    value=RepairForkAction.AUTO_FIX.value,
                    label=RepairForkAction.AUTO_FIX.value,
                )
            )
        options.append(
            SelectOptionDict(
                value=RepairForkAction.DIFFERENT_URL.value,
                label=RepairForkAction.DIFFERENT_URL.value,
            )
        )
        return self.async_show_form(
            step_id="confirm_fork",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "fork_action", default=RepairForkAction.PROCEED.value
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                            translation_key="repair_fork_action",
                        )
                    )
                }
            ),
            description_placeholders={
                "name": self.blueprint_name,
                "fork_notice": fork_notice,
                "diff_text": self._pending_diff or "No changes",
            },
        )

    async def _async_execute_fork_switch(self) -> data_entry_flow.FlowResult:
        """Execute fork update in-place preserving filesystem path.

        Returns:
            FlowResult completing flow or displaying error.

        """
        if (
            self._pending_precondition is None
            or not self._pending_precondition.must_exist
            or not self._pending_precondition.content_hash
        ):
            return await self.async_step_change_url(errors={"url": RepairError.INVALID_URL.value})

        url = self._pending_url or ""
        raw_content = self._pending_content or ""
        old_runtime_url = self.coordinator.data.get(self.path, {}).get("source_url")
        has_persisted = self.relative_path in self.coordinator._persisted_metadata
        old_persisted_url = self.coordinator._persisted_metadata.get(self.relative_path, {}).get(
            "source_url"
        )

        if url:
            if self.path not in self.coordinator.data:
                self.coordinator.data[self.path] = {}
            self.coordinator.data[self.path]["source_url"] = url
            persisted = self.coordinator._persisted_metadata.setdefault(self.relative_path, {})
            persisted["source_url"] = url

        filter_paths = self.coordinator.get_selector_filter_paths()
        fork_content_with_url = ensure_source_url(raw_content, url, filter_paths=filter_paths)

        try:
            await self.coordinator.async_install_blueprint(
                self.path,
                fork_content_with_url,
                reload_services=True,
                backup=True,
                source_url=url,
                file_precondition=self._pending_precondition,
            )
        except Exception as err:
            if url:
                if old_runtime_url is not None:
                    self.coordinator.data.setdefault(self.path, {})["source_url"] = old_runtime_url
                elif self.path in self.coordinator.data:
                    self.coordinator.data[self.path].pop("source_url", None)

                if has_persisted:
                    if old_persisted_url is not None:
                        self.coordinator._persisted_metadata[self.relative_path]["source_url"] = (
                            old_persisted_url
                        )
                    else:
                        self.coordinator._persisted_metadata[self.relative_path].pop(
                            "source_url", None
                        )
                else:
                    self.coordinator._persisted_metadata.pop(self.relative_path, None)

            return self.async_show_form(
                step_id="confirm_fork",
                data_schema=vol.Schema({}),
                errors={"base": str(err)},
            )

        persisted = self.coordinator._persisted_metadata.setdefault(self.relative_path, {})
        persisted["source_url"] = url
        await self.coordinator.async_unpin_blueprint(self.relative_path, self.path)
        if self.path in self.coordinator.data:
            self.coordinator.data[self.path]["source_url"] = url

        self._delete_current_issue()
        await self.coordinator.async_request_refresh()
        return self.async_create_entry(title="", data={})

    async def async_step_unpin(
        self, user_input: dict[str, object] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle unpinning a blueprint.

        Args:
            user_input: Optional user input dictionary.

        Returns:
            FlowResult confirming unpin or executing unpin.

        """
        if user_input is None:
            return self.async_show_form(
                step_id="unpin",
                data_schema=vol.Schema({vol.Required("confirm_unpin", default=False): bool}),
                description_placeholders={
                    "name": self.blueprint_name,
                    "path": self.relative_path,
                    "source_url": self.source_url or "upstream author",
                },
            )
        if not user_input.get("confirm_unpin"):
            return self.async_show_form(
                step_id="unpin",
                data_schema=vol.Schema({vol.Required("confirm_unpin", default=False): bool}),
                errors={"base": "confirmation_required"},
                description_placeholders={
                    "name": self.blueprint_name,
                    "path": self.relative_path,
                    "source_url": self.source_url or "upstream author",
                },
            )
        await self.coordinator.async_unpin_blueprint(self.relative_path, self.path)
        self._delete_current_issue()
        await self.coordinator.async_request_refresh()
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: Mapping[str, object] | None,
) -> RepairsFlow:
    """Create a repair fix flow.

    Args:
        hass: HomeAssistant instance.
        issue_id: Identifier of the repair issue.
        data: Issue data dictionary.

    Returns:
        RepairsFlow instance for the issue.

    Raises:
        UnknownFlow: If issue data is missing or coordinator cannot be found.

    """
    if not data:
        raise data_entry_flow.UnknownFlow("Missing required issue data")

    config_entry_id_raw = data.get("config_entry_id")
    config_entry_id: str | None = (
        str(config_entry_id_raw) if isinstance(config_entry_id_raw, str) else None
    )
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

    if issue_id.startswith(RepairIssueType.INCOMPATIBLE_BLUEPRINT.value) or (
        data and data.get("issue_type") == RepairIssueType.INCOMPATIBLE_BLUEPRINT.value
    ):
        return IncompatibleBlueprintRepairFlow(coordinator, issue_id, data)

    return WithdrawnBlueprintRepairFlow(coordinator, issue_id, data)
