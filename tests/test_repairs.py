"""Unit and integration tests for Blueprints Updater repairs flow and issue management."""

from datetime import timedelta
from http import HTTPStatus
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant import data_entry_flow
from homeassistant.exceptions import HomeAssistantError

from custom_components.blueprints_updater.const import (
    CONF_FILTER_MODE,
    CONF_SELECTED_BLUEPRINTS,
    DOMAIN,
    FilterMode,
    FunctionalDomain,
    RepairAction,
    RepairIssueType,
    RepairRiskAction,
)
from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator
from custom_components.blueprints_updater.file_store import (
    BlueprintFileStore,
)
from custom_components.blueprints_updater.repairs import (
    WithdrawnBlueprintRepairFlow,
    async_create_fix_flow,
)


@pytest.fixture
def hass(_mock_hass):
    """Aliasing _mock_hass to hass for test_repairs."""
    return _mock_hass


@pytest.fixture
def coordinator(hass, monkeypatch) -> BlueprintUpdateCoordinator:
    """Fixture for BlueprintUpdateCoordinator."""
    entry = MagicMock()
    entry.options = MappingProxyType({})
    entry.data = {}
    coord = BlueprintUpdateCoordinator(
        hass,
        entry,
        timedelta(hours=24),
    )

    def _mock_set_data(data: dict) -> None:
        coord.data = data

    monkeypatch.setattr(coord, "async_set_updated_data", MagicMock(side_effect=_mock_set_data))
    monkeypatch.setattr(coord, "async_update_listeners", MagicMock())
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    monkeypatch.setattr(coord, "async_reconcile_reload_services", AsyncMock())
    coord.setup_complete = True
    coord.last_update_success = True
    monkeypatch.setattr(coord, "_is_safe_path", MagicMock(return_value=True))
    monkeypatch.setattr(coord, "_is_safe_url", AsyncMock(return_value=True))
    return coord


@pytest.fixture
def mock_repair_flow(coordinator, hass):
    """Fixture for WithdrawnBlueprintRepairFlow."""
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = {
        CONF_FILTER_MODE: FilterMode.ALL,
        CONF_SELECTED_BLUEPRINTS: [],
    }
    issue_data = {
        "path": "/config/blueprints/automation/test/blueprint.yaml",
        "relative_path": "automation/test/blueprint.yaml",
        "domain": FunctionalDomain.AUTOMATION,
        "name": "Test Blueprint",
        "source_url": "https://github.com/example/repo/blob/main/test.yaml",
    }
    flow = WithdrawnBlueprintRepairFlow(
        coordinator,
        issue_id="withdrawn_blueprint_12345",
        data=issue_data,
    )
    flow.hass = hass
    return flow


@pytest.mark.asyncio
async def test_repair_flow_init_menu(mock_repair_flow):
    """Test initial menu presentation in repair flow."""
    result = await mock_repair_flow.async_step_init()
    assert result.get("type") == data_entry_flow.FlowResultType.MENU
    assert result.get("step_id") == "init"
    assert result.get("menu_options") == [
        RepairAction.CHANGE_URL,
        RepairAction.STOP_TRACKING,
        RepairAction.DELETE_BLUEPRINT,
    ]


@pytest.mark.asyncio
async def test_repair_flow_initialization_validation_and_fallbacks(coordinator, hass):
    """Test repair flow aborts on missing required fields and applies optional defaults."""
    flow_none = WithdrawnBlueprintRepairFlow(coordinator, "issue_none", None)
    result_none = await flow_none.async_step_init()
    assert result_none.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_none.get("reason") == "missing_issue_data"

    flow_empty = WithdrawnBlueprintRepairFlow(coordinator, "issue_empty", {})
    result_empty = await flow_empty.async_step_init()
    assert result_empty.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_empty.get("reason") == "missing_issue_data"

    flow_partial = WithdrawnBlueprintRepairFlow(
        coordinator, "issue_partial", {"path": "/config/test.yaml"}
    )
    result_partial = await flow_partial.async_step_init()
    assert result_partial.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_partial.get("reason") == "missing_issue_data"

    flow_partial2 = WithdrawnBlueprintRepairFlow(
        coordinator, "issue_partial2", {"relative_path": "automation/test.yaml"}
    )
    result_partial2 = await flow_partial2.async_step_init()
    assert result_partial2.get("type") == data_entry_flow.FlowResultType.ABORT
    assert result_partial2.get("reason") == "missing_issue_data"

    # Valid required fields with missing optional fields -> applies defaults
    flow = WithdrawnBlueprintRepairFlow(
        coordinator,
        "issue_valid",
        {
            "path": "/config/blueprints/automation/test.yaml",
            "relative_path": "automation/test.yaml",
        },
    )
    assert flow.path == "/config/blueprints/automation/test.yaml"
    assert flow.relative_path == "automation/test.yaml"
    assert flow.domain == FunctionalDomain.AUTOMATION
    assert flow.blueprint_name == "automation/test.yaml"
    assert flow.source_url == ""


@pytest.mark.asyncio
async def test_stop_tracking_filter_mode_all(mock_repair_flow, hass):
    """Test stop tracking action when current filter mode is ALL."""
    mock_repair_flow.coordinator.config_entry.options = {
        CONF_FILTER_MODE: FilterMode.ALL,
        CONF_SELECTED_BLUEPRINTS: [],
    }
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete:
        result = await mock_repair_flow.async_step_stop_tracking()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_delete.assert_called_once_with(hass, DOMAIN, "withdrawn_blueprint_12345")

    hass.config_entries.async_update_entry.assert_called_once()
    _call_args = hass.config_entries.async_update_entry.call_args[1]
    assert _call_args["options"][CONF_FILTER_MODE] == FilterMode.BLACKLIST
    assert "automation/test/blueprint.yaml" in _call_args["options"][CONF_SELECTED_BLUEPRINTS]


@pytest.mark.asyncio
async def test_stop_tracking_filter_mode_whitelist(mock_repair_flow, hass):
    """Test stop tracking action when current filter mode is WHITELIST."""
    mock_repair_flow.coordinator.config_entry.options = {
        CONF_FILTER_MODE: FilterMode.WHITELIST,
        CONF_SELECTED_BLUEPRINTS: ["automation/test/blueprint.yaml", "automation/other.yaml"],
    }
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete:
        result = await mock_repair_flow.async_step_stop_tracking()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_delete.assert_called_once()

    _call_args = hass.config_entries.async_update_entry.call_args[1]
    assert _call_args["options"][CONF_FILTER_MODE] == FilterMode.WHITELIST
    assert "automation/test/blueprint.yaml" not in _call_args["options"][CONF_SELECTED_BLUEPRINTS]
    assert "automation/other.yaml" in _call_args["options"][CONF_SELECTED_BLUEPRINTS]


@pytest.mark.asyncio
async def test_stop_tracking_filter_mode_blacklist(mock_repair_flow, hass):
    """Test stop tracking action when current filter mode is BLACKLIST."""
    mock_repair_flow.coordinator.config_entry.options = {
        CONF_FILTER_MODE: FilterMode.BLACKLIST,
        CONF_SELECTED_BLUEPRINTS: ["automation/other.yaml"],
    }
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete:
        result = await mock_repair_flow.async_step_stop_tracking()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_delete.assert_called_once()

    _call_args = hass.config_entries.async_update_entry.call_args[1]
    assert _call_args["options"][CONF_FILTER_MODE] == FilterMode.BLACKLIST
    assert "automation/test/blueprint.yaml" in _call_args["options"][CONF_SELECTED_BLUEPRINTS]
    assert "automation/other.yaml" in _call_args["options"][CONF_SELECTED_BLUEPRINTS]


@pytest.mark.asyncio
async def test_stop_tracking_without_config_entry(mock_repair_flow, hass):
    """Test stop tracking handles missing config entry gracefully."""
    mock_repair_flow.coordinator.config_entry = None
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete:
        result = await mock_repair_flow.async_step_stop_tracking()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_delete.assert_called_once_with(hass, DOMAIN, "withdrawn_blueprint_12345")


@pytest.mark.asyncio
async def test_change_url_validation_failures(mock_repair_flow):
    """Test change_url step handling empty, whitespace, and invalid URLs."""
    # Empty URL
    result = await mock_repair_flow.async_step_change_url({"url": ""})
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("errors") == {"url": "missing_url"}

    # Whitespace-only URL
    result_ws = await mock_repair_flow.async_step_change_url({"url": "   \n\t   "})
    assert result_ws.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_ws.get("errors") == {"url": "missing_url"}

    # Specific network / validation exceptions convert to invalid_url
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    result_err = await mock_repair_flow.async_step_change_url(
        {"url": "https://invalid.url/test.yaml"}
    )
    assert result_err.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_err.get("errors") == {"url": "invalid_url"}

    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        side_effect=HomeAssistantError("Invalid blueprint YAML")
    )
    result_err_ha = await mock_repair_flow.async_step_change_url(
        {"url": "https://invalid.url/test.yaml"}
    )
    assert result_err_ha.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_err_ha.get("errors") == {"url": "invalid_url"}

    # Unexpected programming error propagates
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        side_effect=RuntimeError("Unexpected memory crash")
    )
    with pytest.raises(RuntimeError, match="Unexpected memory crash"):
        await mock_repair_flow.async_step_change_url({"url": "https://invalid.url/test.yaml"})


@pytest.mark.asyncio
async def test_change_url_clean_success(mock_repair_flow, hass):
    """Test change_url step with clean URL transitions to review step before install."""
    content = "blueprint:\n  name: New\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(return_value=[])
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()
    mock_repair_flow.coordinator.data = {
        mock_repair_flow.path: {"source_url": "https://old.url/test.yaml"}
    }

    # Step 1: change_url presents review form
    result = await mock_repair_flow.async_step_change_url({"url": "https://valid.url/test.yaml"})
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "confirm_risks"
    assert "description_placeholders" in result
    risk_placeholders = result["description_placeholders"]
    assert "risk_report" in risk_placeholders
    assert isinstance(risk_placeholders["risk_report"], str)
    assert risk_placeholders["risk_report"]

    # Step 2: user reviews and proceeds
    with (
        patch.object(
            mock_repair_flow.coordinator, "async_install_blueprint", new_callable=AsyncMock
        ) as mock_install,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete,
    ):
        result2 = await mock_repair_flow.async_step_confirm_risks(
            {"risk_action": RepairRiskAction.PROCEED}
        )
        assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_install.assert_awaited_once_with(
            mock_repair_flow.path,
            content,
            reload_services=False,
            backup=True,
            source_url=canonical_url,
            file_precondition=mock_repair_flow._pending_precondition,
        )
        mock_delete.assert_called_once_with(hass, DOMAIN, "withdrawn_blueprint_12345")
        assert (
            mock_repair_flow.coordinator.data[mock_repair_flow.path]["source_url"] == canonical_url
        )


@pytest.mark.asyncio
async def test_change_url_with_risks_and_confirm(mock_repair_flow, hass):
    """Test change_url step detecting breaking risks and user confirming update."""
    content = "blueprint:\n  name: New\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(
        return_value=[{"type": "new_mandatory", "args": {"input": "delay"}}]
    )
    mock_repair_flow.coordinator.async_summarize_risks = AsyncMock(
        return_value="New mandatory input delay"
    )
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()

    # Step transitions to confirm_risks form
    result = await mock_repair_flow.async_step_change_url({"url": "https://valid.url/test.yaml"})
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "confirm_risks"

    # User chooses to proceed
    with (
        patch.object(
            mock_repair_flow.coordinator, "async_install_blueprint", new_callable=AsyncMock
        ) as mock_install,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete,
    ):
        result2 = await mock_repair_flow.async_step_confirm_risks(
            {"risk_action": RepairRiskAction.PROCEED}
        )
        assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_install.assert_awaited_once_with(
            mock_repair_flow.path,
            content,
            reload_services=False,
            backup=True,
            source_url=canonical_url,
            file_precondition=mock_repair_flow._pending_precondition,
        )
        mock_delete.assert_called_once_with(hass, DOMAIN, "withdrawn_blueprint_12345")


@pytest.mark.asyncio
async def test_change_url_with_risks_branching(mock_repair_flow):
    """Test branching choices on confirm_risks step."""
    mock_repair_flow._pending_url = "https://valid.url/test.yaml"
    mock_repair_flow._detected_risks = [{"type": "system_error"}]
    mock_repair_flow.coordinator.async_summarize_risks = AsyncMock(return_value="Risk summary")

    # None user input renders the form
    result_none = await mock_repair_flow.async_step_confirm_risks(None)
    assert result_none.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_none.get("step_id") == "confirm_risks"

    # Try different URL branch
    result = await mock_repair_flow.async_step_confirm_risks(
        {"risk_action": RepairRiskAction.DIFFERENT_URL}
    )
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "change_url"

    # Stop tracking branch
    with patch.object(
        mock_repair_flow, "_async_execute_stop_tracking", new_callable=AsyncMock
    ) as mock_stop:
        mock_stop.return_value = {"type": data_entry_flow.FlowResultType.CREATE_ENTRY}
        result2 = await mock_repair_flow.async_step_confirm_risks(
            {"risk_action": RepairRiskAction.STOP_TRACKING}
        )
        assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        mock_stop.assert_called_once()

    # Proceed branch with missing pending state returns to change_url with error
    mock_repair_flow._pending_content = None
    mock_repair_flow._pending_canonical_url = None
    result_missing = await mock_repair_flow.async_step_confirm_risks(
        {"risk_action": RepairRiskAction.PROCEED}
    )
    assert result_missing.get("type") == data_entry_flow.FlowResultType.FORM
    assert result_missing.get("step_id") == "change_url"
    assert result_missing.get("errors") == {"url": "invalid_url"}


@pytest.mark.asyncio
async def test_delete_blueprint_unused(mock_repair_flow):
    """Test deleting blueprint when not used by any entities."""
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()
    # Test FunctionalDomain.SCRIPT
    mock_repair_flow.domain = FunctionalDomain.SCRIPT
    with (
        patch("os.path.isfile", return_value=True),
        patch("os.remove") as mock_remove,
        patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    ):
        await mock_repair_flow._async_execute_delete()
        mock_remove.assert_called_once()
        mock_repair_flow.coordinator.async_reconcile_reload_services.assert_awaited_with(
            {FunctionalDomain.SCRIPT}
        )

    # Test FunctionalDomain.TEMPLATE
    mock_repair_flow.domain = FunctionalDomain.TEMPLATE
    with (
        patch("os.path.isfile", return_value=True),
        patch("os.remove") as mock_remove,
        patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    ):
        await mock_repair_flow._async_execute_delete()
        mock_remove.assert_called_once()
        mock_repair_flow.coordinator.async_reconcile_reload_services.assert_awaited_with(
            {FunctionalDomain.TEMPLATE}
        )

    # When step is shown for unused blueprint, take_control_tip is empty string
    with patch(
        "custom_components.blueprints_updater.utils.automations_with_blueprint",
        return_value=[],
    ):
        mock_repair_flow.domain = FunctionalDomain.AUTOMATION
        res_unused = await mock_repair_flow.async_step_delete_blueprint(None)
        assert res_unused.get("type") == data_entry_flow.FlowResultType.FORM
        placeholders = res_unused.get("description_placeholders", {})
        assert placeholders.get("usage_count") == "0"
        assert placeholders.get("entities") == "None"
        assert placeholders.get("take_control_tip") == ""


@pytest.mark.asyncio
async def test_delete_blueprint_domains_and_usage_error(coordinator, hass):
    """Test delete blueprint usage checks across script and template domains and error handling."""
    # Test FunctionalDomain.SCRIPT
    flow_script = WithdrawnBlueprintRepairFlow(
        coordinator,
        "issue_script",
        {
            "relative_path": "script/test.yaml",
            "domain": FunctionalDomain.SCRIPT,
            "path": "/config/blueprints/script/test.yaml",
        },
    )
    flow_script.hass = hass
    flow_script.coordinator.async_translate = AsyncMock(return_value="Script Take Control Tip")
    with patch(
        "custom_components.blueprints_updater.utils.scripts_with_blueprint",
        return_value=["script.test_script"],
    ):
        res = await flow_script.async_step_delete_blueprint(None)
        assert res.get("type") == data_entry_flow.FlowResultType.FORM
        assert (schema := res.get("data_schema")) is not None
        assert "confirm_delete_in_use" in schema.schema
        assert isinstance(ph_script := res.get("description_placeholders"), dict)
        assert ph_script.get("take_control_tip") == "\n\nScript Take Control Tip"

    # Test FunctionalDomain.TEMPLATE
    flow_tpl = WithdrawnBlueprintRepairFlow(
        coordinator,
        "issue_tpl",
        {
            "relative_path": "template/test.yaml",
            "domain": FunctionalDomain.TEMPLATE,
            "path": "/config/blueprints/template/test.yaml",
        },
    )
    flow_tpl.hass = hass
    flow_tpl.coordinator.async_translate = AsyncMock(return_value="Template Take Control Tip")
    with patch(
        "custom_components.blueprints_updater.utils.templates_with_blueprint",
        return_value=["template.test_sensor"],
    ):
        res_tpl = await flow_tpl.async_step_delete_blueprint(None)
        assert res_tpl.get("type") == data_entry_flow.FlowResultType.FORM
        assert (schema_tpl := res_tpl.get("data_schema")) is not None
        assert "confirm_delete_in_use" in schema_tpl.schema
        assert isinstance(ph_tpl := res_tpl.get("description_placeholders"), dict)
        assert ph_tpl.get("take_control_tip") == "\n\nTemplate Take Control Tip"

    # Test usage calculation raising unexpected exception
    flow_err = WithdrawnBlueprintRepairFlow(
        coordinator,
        "issue_err",
        {
            "relative_path": "automation/test.yaml",
            "domain": FunctionalDomain.AUTOMATION,
            "path": "/config/blueprints/automation/test.yaml",
        },
    )
    flow_err.hass = hass
    with (
        patch(
            "custom_components.blueprints_updater.utils.automations_with_blueprint",
            side_effect=HomeAssistantError("Database error"),
        ),
    ):
        # Usage discovery failure must block deletion and allow a retry.
        res_err = await flow_err.async_step_delete_blueprint({})
        assert res_err.get("type") == data_entry_flow.FlowResultType.FORM
        assert res_err.get("errors") == {"base": "usage_discovery_failed"}
        assert isinstance(ph_err := res_err.get("description_placeholders"), dict)
        assert ph_err.get("take_control_tip") == ""


@pytest.mark.asyncio
async def test_delete_blueprint_removes_exact_backups(mock_repair_flow, tmp_path, hass):
    """Test delete blueprint cleans up exact numbered backup files from disk."""
    bp_file = tmp_path / "test_bp.yaml"
    bp_file.write_text("blueprint:\n  name: Test\n", encoding="utf-8")
    bak1 = tmp_path / "test_bp.yaml.bak.1"
    bak1.write_text("backup 1", encoding="utf-8")
    bak2 = tmp_path / "test_bp.yaml.bak.2"
    bak2.write_text("backup 2", encoding="utf-8")
    other_file = tmp_path / "other.yaml"
    other_file.write_text("keep me", encoding="utf-8")
    other_bak = tmp_path / "test_bp.yaml.bak.tmp"
    other_bak.write_text("keep me too", encoding="utf-8")

    mock_repair_flow.path = str(bp_file)
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()

    with patch("homeassistant.helpers.issue_registry.async_delete_issue"):
        result = await mock_repair_flow._async_execute_delete()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert not bp_file.exists()
    assert not bak1.exists()
    assert not bak2.exists()
    assert other_file.exists()
    assert other_bak.exists()


@pytest.mark.asyncio
async def test_delete_blueprint_in_use_confirmation(mock_repair_flow, hass):
    """Test deleting blueprint in use requires explicit confirmation and provides placeholders."""
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()

    with patch(
        "custom_components.blueprints_updater.utils.automations_with_blueprint",
        return_value=["automation.living_room_light"],
    ):
        # Initial form presentation without input
        mock_repair_flow.coordinator.async_translate = AsyncMock(
            return_value="Tip: You can 'Take Control' of dependent automations"
        )
        res_initial = await mock_repair_flow.async_step_delete_blueprint(None)
        assert res_initial.get("type") == data_entry_flow.FlowResultType.FORM
        assert res_initial.get("step_id") == "delete_blueprint"
        assert isinstance(placeholders := res_initial.get("description_placeholders"), dict)
        assert placeholders.get("usage_count") == "1"
        assert placeholders.get("entities") == "automation.living_room_light"
        assert placeholders.get("name") == "Test Blueprint"
        assert placeholders.get("path") == "automation/test/blueprint.yaml"
        assert (
            placeholders.get("take_control_tip")
            == "\n\nTip: You can 'Take Control' of dependent automations"
        )

        # Without confirmation checkbox -> error
        result = await mock_repair_flow.async_step_delete_blueprint(
            {"confirm_delete_in_use": False}
        )
        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("errors") == {"confirm_delete_in_use": "confirmation_required"}

        # With confirmation checkbox -> executes delete
        with (
            patch("os.path.isfile", return_value=True),
            patch("os.remove") as mock_remove,
            patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete,
        ):
            result2 = await mock_repair_flow.async_step_delete_blueprint(
                {"confirm_delete_in_use": True}
            )
            assert result2.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
            mock_remove.assert_called()
            mock_delete.assert_called_once_with(hass, DOMAIN, "withdrawn_blueprint_12345")


@pytest.mark.asyncio
async def test_async_create_fix_flow(coordinator, hass):
    """Test async_create_fix_flow factory function and edge cases."""
    hass.data[DOMAIN] = {"coordinators": {coordinator.config_entry.entry_id: coordinator}}

    # Single coordinator without config_entry_id succeeds
    flow = await async_create_fix_flow(
        hass,
        "withdrawn_blueprint_test",
        {
            "path": "/config/blueprints/automation/test.yaml",
            "relative_path": "automation/test.yaml",
        },
    )
    assert isinstance(flow, WithdrawnBlueprintRepairFlow)
    assert flow.issue_id == "withdrawn_blueprint_test"
    assert flow.coordinator == coordinator

    # Multiple coordinators with config_entry_id selects correct coordinator
    mock_coord2 = MagicMock(spec=BlueprintUpdateCoordinator)
    mock_coord2.config_entry = MagicMock()
    mock_coord2.config_entry.entry_id = "entry_2"
    hass.data[DOMAIN] = {
        "coordinators": {
            coordinator.config_entry.entry_id: coordinator,
            "entry_2": mock_coord2,
        }
    }

    flow2 = await async_create_fix_flow(
        hass,
        "withdrawn_blueprint_test",
        {
            "config_entry_id": "entry_2",
            "path": "/config/blueprints/automation/test.yaml",
            "relative_path": "automation/test.yaml",
        },
    )
    assert isinstance(flow2, WithdrawnBlueprintRepairFlow)
    assert flow2.coordinator == mock_coord2

    # Multiple coordinators without config_entry_id raises UnknownFlow due to ambiguity
    with pytest.raises(data_entry_flow.UnknownFlow, match="either provide config_entry_id"):
        await async_create_fix_flow(
            hass,
            "withdrawn_blueprint_test",
            {
                "path": "/config/blueprints/automation/test.yaml",
                "relative_path": "automation/test.yaml",
            },
        )

    # Specific config_entry_id not found raises UnknownFlow with entry ID
    with pytest.raises(data_entry_flow.UnknownFlow, match="for config entry entry_unknown"):
        await async_create_fix_flow(
            hass,
            "withdrawn_blueprint_test",
            {
                "config_entry_id": "entry_unknown",
                "path": "/config/blueprints/automation/test.yaml",
                "relative_path": "automation/test.yaml",
            },
        )

    # When no coordinator is found in coordinators dict
    hass.data[DOMAIN] = {"coordinators": {}}
    with pytest.raises(data_entry_flow.UnknownFlow, match="No active coordinator found"):
        await async_create_fix_flow(
            hass,
            "withdrawn_blueprint_test",
            {
                "path": "/config/blueprints/automation/test.yaml",
                "relative_path": "automation/test.yaml",
            },
        )

    # When DOMAIN key is missing completely
    hass.data.pop(DOMAIN, None)
    with pytest.raises(data_entry_flow.UnknownFlow, match="No active coordinator found"):
        await async_create_fix_flow(
            hass,
            "withdrawn_blueprint_test",
            {
                "path": "/config/blueprints/automation/test.yaml",
                "relative_path": "automation/test.yaml",
            },
        )

    # When required data fields are missing
    hass.data[DOMAIN] = {"coordinators": {coordinator.config_entry.entry_id: coordinator}}
    with pytest.raises(data_entry_flow.UnknownFlow, match="Missing required issue data"):
        await async_create_fix_flow(hass, "withdrawn_blueprint_test", {})

    with pytest.raises(data_entry_flow.UnknownFlow, match="Missing required issue data"):
        await async_create_fix_flow(hass, "withdrawn_blueprint_test", None)


@pytest.mark.asyncio
async def test_delete_blueprint_handles_os_error(mock_repair_flow, caplog):
    """Test that file deletion logs warnings on OSError instead of raising unhandled exception."""
    mock_repair_flow.path = "/config/blueprints/automation/test.yaml"
    mock_repair_flow.coordinator.async_reconcile_reload_services = AsyncMock()
    mock_repair_flow.coordinator.async_request_refresh = AsyncMock()

    with (
        patch("os.path.isfile", return_value=True),
        patch("os.remove", side_effect=OSError("Permission denied")),
        patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    ):
        result = await mock_repair_flow._async_execute_delete()
        assert result.get("type") == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert "Failed to remove blueprint file" in caplog.text


def test_get_withdrawn_issue_id_determinism():
    """Test get_withdrawn_issue_id determinism, domain isolation, and unicode safety."""
    id1 = BlueprintUpdateCoordinator.get_withdrawn_issue_id("automation/test.yaml")
    id2 = BlueprintUpdateCoordinator.get_withdrawn_issue_id("automation/test.yaml")
    assert id1 == id2
    assert id1.startswith(f"{RepairIssueType.WITHDRAWN_BLUEPRINT}_")
    assert len(id1) == len(RepairIssueType.WITHDRAWN_BLUEPRINT) + 1 + 16

    # Test domain distinction for same filename
    id_auto = BlueprintUpdateCoordinator.get_withdrawn_issue_id("test.yaml", domain="automation")
    id_script = BlueprintUpdateCoordinator.get_withdrawn_issue_id("test.yaml", domain="script")
    assert id_auto != id_script
    assert id_auto == BlueprintUpdateCoordinator.get_withdrawn_issue_id("automation/test.yaml")
    assert id_script == BlueprintUpdateCoordinator.get_withdrawn_issue_id("script/test.yaml")

    # Test FunctionalDomain enum support
    id_enum_auto = BlueprintUpdateCoordinator.get_withdrawn_issue_id(
        "test.yaml", domain=FunctionalDomain.AUTOMATION
    )
    assert id_enum_auto == id_auto
    id_enum_script = BlueprintUpdateCoordinator.get_withdrawn_issue_id(
        "test.yaml", domain=FunctionalDomain.SCRIPT
    )
    assert id_enum_script == id_script

    # Test Unicode path
    id_unicode = BlueprintUpdateCoordinator.get_withdrawn_issue_id(
        "automation/tự_động_hóa/đèn_bàn.yaml"
    )
    assert id_unicode.startswith(f"{RepairIssueType.WITHDRAWN_BLUEPRINT}_")


@pytest.mark.asyncio
async def test_coordinator_creates_withdrawn_issue_on_404_and_410(coordinator, hass):
    """Test coordinator creates repair issue on both HTTP 404 and HTTP 410."""
    path = "/config/blueprints/automation/author/test.yaml"
    info = {
        "name": "Test Blueprint",
        "relative_path": "automation/author/test.yaml",
        "source_url": "https://github.com/author/repo/raw/main/test.yaml",
        "domain": FunctionalDomain.AUTOMATION,
    }
    coordinator.data = {path: info.copy()}

    # HTTP 404
    mock_req_404 = httpx.Request("GET", "https://github.com/author/repo/raw/main/test.yaml")
    mock_resp_404 = httpx.Response(status_code=HTTPStatus.NOT_FOUND, request=mock_req_404)
    err_404 = httpx.HTTPStatusError("404", request=mock_req_404, response=mock_resp_404)

    with (
        patch.object(coordinator, "_async_fetch_content", side_effect=err_404),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create_404,
    ):
        await coordinator._async_update_blueprint_in_place(
            MagicMock(), path, info, results_to_notify=[], updated_domains=set()
        )
        mock_create_404.assert_called_once()
        assert mock_create_404.call_args[1]["translation_placeholders"]["status_code"] == "404"

    # HTTP 410 (Gone)
    mock_req_410 = httpx.Request("GET", "https://github.com/author/repo/raw/main/test.yaml")
    mock_resp_410 = httpx.Response(status_code=HTTPStatus.GONE, request=mock_req_410)
    err_410 = httpx.HTTPStatusError("410", request=mock_req_410, response=mock_resp_410)

    with (
        patch.object(coordinator, "_async_fetch_content", side_effect=err_410),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create_410,
    ):
        await coordinator._async_update_blueprint_in_place(
            MagicMock(), path, info, results_to_notify=[], updated_domains=set()
        )
        mock_create_410.assert_called_once()
        assert mock_create_410.call_args[1]["translation_placeholders"]["status_code"] == "410"


@pytest.mark.asyncio
async def test_coordinator_no_issue_on_transient_errors(coordinator, hass):
    """Test coordinator does not raise repair issues on transient network or 5xx errors."""
    path = "/config/blueprints/automation/author/test.yaml"
    info = {
        "name": "Test Blueprint",
        "relative_path": "automation/author/test.yaml",
        "source_url": "https://github.com/author/repo/raw/main/test.yaml",
        "domain": FunctionalDomain.AUTOMATION,
    }
    coordinator.data = {path: info.copy()}

    transient_errors = [
        httpx.HTTPStatusError(
            "500",
            request=MagicMock(),
            response=httpx.Response(
                HTTPStatus.INTERNAL_SERVER_ERROR, request=httpx.Request("GET", "https://ex.com")
            ),
        ),
        httpx.HTTPStatusError(
            "503",
            request=MagicMock(),
            response=httpx.Response(
                HTTPStatus.SERVICE_UNAVAILABLE, request=httpx.Request("GET", "https://ex.com")
            ),
        ),
        httpx.HTTPStatusError(
            "429",
            request=MagicMock(),
            response=httpx.Response(
                HTTPStatus.TOO_MANY_REQUESTS, request=httpx.Request("GET", "https://ex.com")
            ),
        ),
        httpx.ConnectTimeout("Timeout"),
        httpx.ConnectError("DNS failure"),
        HomeAssistantError("HA internal error"),
    ]

    for err in transient_errors:
        with (
            patch.object(coordinator, "_async_fetch_content", side_effect=err),
            patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create,
        ):
            await coordinator._async_update_blueprint_in_place(
                MagicMock(), path, info, results_to_notify=[], updated_domains=set()
            )
            mock_create.assert_not_called()
            assert coordinator.data[path]["last_error"] is not None
            assert coordinator.data[path]["last_error"].startswith("fetch_error")


@pytest.mark.asyncio
async def test_coordinator_deletes_withdrawn_issue_on_recovery(coordinator, hass):
    """Test coordinator deletes repair issue when blueprint fetch recovers."""
    path = "/config/blueprints/automation/author/test.yaml"
    info = {
        "name": "Test Blueprint",
        "relative_path": "automation/author/test.yaml",
        "source_url": "https://github.com/author/repo/raw/main/test.yaml",
        "domain": FunctionalDomain.AUTOMATION,
    }
    coordinator.data = {path: info.copy()}

    # 1. Empty content does NOT delete issue
    with (
        patch.object(coordinator, "_async_fetch_content", return_value=("", "etag1", "mod1")),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete_issue,
    ):
        await coordinator._async_update_blueprint_in_place(
            MagicMock(), path, info, results_to_notify=[], updated_domains=set()
        )
        mock_delete_issue.assert_not_called()

    # 2. Invalid blueprint content (syntax/schema error) does NOT delete issue
    with (
        patch.object(
            coordinator,
            "_async_fetch_content",
            return_value=("not_a_valid_blueprint: 123", "etag1", "mod1"),
        ),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete_issue,
    ):
        await coordinator._async_update_blueprint_in_place(
            MagicMock(), path, info, results_to_notify=[], updated_domains=set()
        )
        mock_delete_issue.assert_not_called()

    # 3. Valid blueprint content DOES delete issue
    valid_content = "blueprint:\n  name: Test\n  domain: automation\n"
    with (
        patch.object(
            coordinator, "_async_fetch_content", return_value=(valid_content, "etag1", "mod1")
        ),
        patch.object(coordinator, "_detect_risks_for_update", return_value=[]),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete_issue,
    ):
        await coordinator._async_update_blueprint_in_place(
            MagicMock(),
            path,
            info,
            results_to_notify=[],
            updated_domains=set(),
        )

        mock_delete_issue.assert_called_with(
            hass,
            DOMAIN,
            coordinator.get_withdrawn_issue_id("automation/author/test.yaml"),
        )


@pytest.mark.asyncio
async def test_coordinator_prune_stale_metadata_cleans_repair_issues(coordinator, hass):
    """Test that pruning stale metadata deletes orphaned repair issues."""
    coordinator._persisted_metadata = {
        "automation/deleted_bp.yaml": {"name": "Deleted"},
        "automation/kept_bp.yaml": {"name": "Kept"},
    }
    coordinator.data = {
        "/config/blueprints/automation/deleted_bp.yaml": {
            "relative_path": "automation/deleted_bp.yaml"
        },
        "/config/blueprints/automation/kept_bp.yaml": {"relative_path": "automation/kept_bp.yaml"},
    }

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
            side_effect=lambda h, p: "automation/kept_bp.yaml" if "kept_bp" in p else None,
        ),
        patch.object(
            coordinator,
            "_filter_existing_metadata",
            return_value={"automation/kept_bp.yaml": {"name": "Kept"}},
        ),
        patch.object(coordinator, "_async_save_metadata", new_callable=AsyncMock),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete_issue,
    ):
        await coordinator._async_prune_stale_metadata(
            {"/config/blueprints/automation/kept_bp.yaml"}
        )

        mock_delete_issue.assert_called_with(
            hass,
            DOMAIN,
            coordinator.get_withdrawn_issue_id("automation/deleted_bp.yaml"),
        )


def test_blueprint_file_store_remove_blueprint_and_backups(tmp_path, caplog):
    """Test BlueprintFileStore.remove_blueprint_and_backups removes file and backups."""
    bp_file = tmp_path / "my_bp.yaml"
    bp_file.write_text("content", encoding="utf-8")
    bak1 = tmp_path / "my_bp.yaml.bak.1"
    bak1.write_text("bak 1", encoding="utf-8")
    bak2 = tmp_path / "my_bp.yaml.bak.2"
    bak2.write_text("bak 2", encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text("other", encoding="utf-8")

    BlueprintFileStore.remove_blueprint_and_backups(str(bp_file))
    assert not bp_file.exists()
    assert not bak1.exists()
    assert not bak2.exists()
    assert other.exists()

    # Test removing non-existent path doesn't fail
    BlueprintFileStore.remove_blueprint_and_backups(str(tmp_path / "nonexistent" / "bp.yaml"))


def test_get_coordinator_for_flow_resolution(hass, coordinator):
    """Test BlueprintUpdateCoordinator.get_coordinator_for_flow helper resolution."""
    # When hass.data is empty
    hass.data = {}
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass) is None

    # When coordinators is empty dict
    hass.data = {DOMAIN: {"coordinators": {}}}
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass) is None

    # When single coordinator exists
    entry_id = coordinator.config_entry.entry_id
    hass.data = {DOMAIN: {"coordinators": {entry_id: coordinator}}}
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass) is coordinator
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, entry_id) is coordinator
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, "unknown") is None

    # When multiple coordinators exist
    other_coord = MagicMock(spec=BlueprintUpdateCoordinator)
    hass.data = {DOMAIN: {"coordinators": {entry_id: coordinator, "entry_2": other_coord}}}
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass) is None
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, entry_id) is coordinator
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, "entry_2") is other_coord

    # When coordinators contain non-coordinator instance
    hass.data = {DOMAIN: {"coordinators": {entry_id: "not_a_coordinator"}}}
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass) is None
    assert BlueprintUpdateCoordinator.get_coordinator_for_flow(hass, entry_id) is None


def test_coordinator_create_issue_unresolvable_relative_path(coordinator, hass):
    """Test _async_create_withdrawn_issue returns safely when relative path cannot be resolved."""
    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_blueprint_relative_path",
            return_value=None,
        ),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_create,
    ):
        coordinator._async_create_withdrawn_issue("/config/unknown.yaml", {})
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_coordinator_public_facades(coordinator):
    """Test public facades async_fetch_import_data and async_detect_risks_for_update."""
    with patch.object(
        coordinator,
        "_async_fetch_import_data",
        new=AsyncMock(return_value=("content", "url", "author", "name", None)),
    ) as mock_fetch:
        res = await coordinator.async_fetch_import_data("https://example.com/test.yaml")
        assert res[0] == "content"
        mock_fetch.assert_called_once_with("https://example.com/test.yaml")

    with patch.object(
        coordinator,
        "_detect_risks_for_update",
        new=AsyncMock(return_value=[]),
    ) as mock_detect:
        risks = await coordinator.async_detect_risks_for_update(
            "/config/path.yaml", {"relative_path": "automation/path.yaml"}, "content"
        )
        assert risks == []
        mock_detect.assert_called_once_with(
            "/config/path.yaml", {"relative_path": "automation/path.yaml"}, "content", session=None
        )


def test_coordinator_create_and_delete_issue_domain_normalization(coordinator, hass):
    """Test create and delete issue generate matching IDs with normalized domain."""
    path = "/config/blueprints/automation/author/test.yaml"
    info = {
        "name": "Test Blueprint",
        "relative_path": "automation/author/test.yaml",
        "source_url": "https://example.com/test.yaml",
        "domain": "automation",
    }
    coordinator.data = {path: info.copy()}
    created_issue_id = None
    deleted_issue_id = None

    def capture_create(*args, **kwargs):
        nonlocal created_issue_id
        created_issue_id = kwargs.get("issue_id")

    def capture_delete(*args, **kwargs):
        nonlocal deleted_issue_id
        deleted_issue_id = args[2] if len(args) > 2 else kwargs.get("issue_id")

    with (
        patch(
            "homeassistant.helpers.issue_registry.async_create_issue",
            side_effect=capture_create,
        ),
        patch(
            "homeassistant.helpers.issue_registry.async_delete_issue",
            side_effect=capture_delete,
        ),
    ):
        coordinator._async_create_withdrawn_issue(path, info, status_code=404)
        coordinator._async_delete_withdrawn_issue(path)

    assert created_issue_id is not None
    assert created_issue_id == deleted_issue_id


@pytest.mark.asyncio
async def test_change_url_with_git_diff_preview(mock_repair_flow):
    """Test change_url generates git diff and includes formatted preview in confirm_risks."""
    content = "blueprint:\n  name: Updated Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(
        return_value=[{"type": "new_mandatory", "args": {"input": "delay"}}]
    )
    mock_repair_flow.coordinator.async_summarize_risks = AsyncMock(
        return_value="New mandatory input delay"
    )
    mock_repair_flow.coordinator.async_translate = AsyncMock(return_value="Code Changes (Git Diff)")

    diff_content = "--- local\n+++ remote\n@@ -1,2 +1,2 @@\n-old\n+new\n"

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._read_and_diff",
        return_value=diff_content,
    ) as mock_diff:
        result = await mock_repair_flow.async_step_change_url(
            {"url": "https://valid.url/test.yaml"}
        )

        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("step_id") == "confirm_risks"
        mock_diff.assert_called_once_with(mock_repair_flow.path, content, canonical_url)
        assert mock_repair_flow._pending_diff == diff_content

        placeholders = result.get("description_placeholders", {})
        assert "risk_report" in placeholders
        risk_report = placeholders["risk_report"]
        assert "<details>" in risk_report
        assert "<summary>Code Changes (Git Diff)</summary>" in risk_report
        assert "```diff" in risk_report
        assert "+new" in risk_report


@pytest.mark.asyncio
async def test_change_url_with_usage_warning_and_safety_in_preview(mock_repair_flow):
    """Test change_url includes usage warning and safety messages in repair preview."""
    content = "blueprint:\n  name: Updated Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(
        return_value=[{"type": "new_mandatory", "args": {"input": "delay"}}]
    )
    mock_repair_flow.coordinator.async_summarize_risks = AsyncMock(
        return_value="New mandatory input delay"
    )

    async def _mock_translate(key: str, **kwargs: object) -> str:
        """Mock translation handler for repair preview strings."""
        if key == "usage_warning":
            cnt = kwargs.get("count")
            dom = kwargs.get("domain")
            url = kwargs.get("usage_url")
            return f"Warning: affects {cnt} {dom}(s) at {url}"
        if key == "update_safety_message":
            return "Safety Tip: enable backups"
        if key == "backup_automatic_notice":
            return "Safety Note: automatic backup will be created"
        if key == "breaking_risks_title":
            return "[WARNING] POTENTIAL BREAKING CHANGES"
        return f"[{key}]"

    mock_repair_flow.coordinator.async_translate = AsyncMock(side_effect=_mock_translate)

    with (
        patch(
            "custom_components.blueprints_updater.coordinator.get_blueprint_usage_entities",
            return_value=["automation.motion_sensor_light", "automation.hallway_light"],
        ),
        patch(
            "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._read_and_diff",
            return_value="",
        ),
    ):
        result = await mock_repair_flow.async_step_change_url(
            {"url": "https://valid.url/test.yaml"}
        )

        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("step_id") == "confirm_risks"

        placeholders = result.get("description_placeholders", {})
        risk_report = placeholders.get("risk_report", "")
        expected_url_warning = (
            "Warning: affects 2 automation(s) at "
            "/config/automation/dashboard?blueprint=test%2Fblueprint.yaml"
        )
        assert expected_url_warning in risk_report
        assert "[WARNING] POTENTIAL BREAKING CHANGES" in risk_report
        assert "New mandatory input delay" in risk_report
        assert "Safety Tip: enable backups" not in risk_report
        assert "Safety Note: automatic backup will be created" in risk_report


@pytest.mark.asyncio
async def test_change_url_git_diff_generation_error_handled(mock_repair_flow):
    """Test change_url handles diff generation errors gracefully without breaking flow."""
    content = "blueprint:\n  name: Updated Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(
        return_value=[{"type": "system_error"}]
    )
    mock_repair_flow.coordinator.async_summarize_risks = AsyncMock(return_value="System error")

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._read_and_diff",
        side_effect=OSError("Disk read failure"),
    ):
        result = await mock_repair_flow.async_step_change_url(
            {"url": "https://valid.url/test.yaml"}
        )

        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("step_id") == "confirm_risks"
        placeholders = result.get("description_placeholders", {})
        assert "risk_report" in placeholders


@pytest.mark.asyncio
async def test_format_diff_preview_formatting_and_fences(mock_repair_flow):
    """Test async_format_diff_block handles various diff inputs and backtick fencing."""
    mock_repair_flow.coordinator.async_translate = AsyncMock(return_value="Diff Title")

    # None and empty strings return empty string
    assert await mock_repair_flow.coordinator.async_format_diff_block(None) == ""
    assert await mock_repair_flow.coordinator.async_format_diff_block("") == ""
    assert await mock_repair_flow.coordinator.async_format_diff_block("   \n\t  ") == ""

    # Standard diff
    formatted = await mock_repair_flow.coordinator.async_format_diff_block("--- a\n+++ b\n")
    assert "<summary>Diff Title</summary>" in formatted
    assert "```diff\n--- a\n+++ b\n```" in formatted

    # Diff containing backticks triggers adaptive fencing
    diff_with_backticks = "--- a\n+++ b\n+```\n+code block\n+```\n"
    formatted_fences = await mock_repair_flow.coordinator.async_format_diff_block(
        diff_with_backticks
    )
    assert "````diff" in formatted_fences
    assert "````\n</details>" in formatted_fences


@pytest.mark.asyncio
async def test_change_url_local_revision_changed_before_proceed_rejected(
    mock_repair_flow, tmp_path
):
    """Test changing local file revision after preview causes PROCEED to reject stale content."""
    bp_file = tmp_path / "blueprint.yaml"
    initial_content = "blueprint:\n  name: Initial Version\n"
    bp_file.write_text(initial_content, encoding="utf-8")

    mock_repair_flow.path = str(bp_file)
    original_source_url = "https://original.source/test.yaml"
    mock_repair_flow.coordinator.data = {mock_repair_flow.path: {"source_url": original_source_url}}
    remote_content = "blueprint:\n  name: Updated Remote Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"

    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(remote_content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(return_value=[])

    # Step 1: change_url captures precondition of initial file
    result = await mock_repair_flow.async_step_change_url({"url": "https://valid.url/test.yaml"})
    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "confirm_risks"
    assert mock_repair_flow._pending_precondition is not None
    assert mock_repair_flow._pending_precondition.content_hash == BlueprintFileStore._hash_file(
        str(bp_file)
    )

    # Concurrently modify the local blueprint file on disk before user proceeds
    concurrent_content = "blueprint:\n  name: Concurrently Modified Local\n"
    bp_file.write_text(concurrent_content, encoding="utf-8")

    # Step 2: User proceeds, real coordinator rejects due to precondition mismatch
    with (
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete,
        pytest.raises(HomeAssistantError, match="Local blueprint changed"),
    ):
        await mock_repair_flow.async_step_confirm_risks({"risk_action": RepairRiskAction.PROCEED})

    # Issue was NOT deleted and file was NOT overwritten with remote content
    mock_delete.assert_not_called()
    assert bp_file.read_text(encoding="utf-8") == concurrent_content
    assert (
        mock_repair_flow.coordinator.data[mock_repair_flow.path]["source_url"]
        == original_source_url
    )


@pytest.mark.asyncio
async def test_change_url_file_modified_during_diff_rejected(mock_repair_flow, tmp_path):
    """Test concurrent local file change during diff generation causes PROCEED rejection."""
    bp_file = tmp_path / "blueprint.yaml"
    initial_content = "blueprint:\n  name: Initial Version\n"
    bp_file.write_text(initial_content, encoding="utf-8")

    mock_repair_flow.path = str(bp_file)
    initial_hash = BlueprintFileStore._hash_file(str(bp_file))
    original_source_url = "https://original.source/test.yaml"
    mock_repair_flow.coordinator.data = {mock_repair_flow.path: {"source_url": original_source_url}}
    remote_content = "blueprint:\n  name: Updated Remote Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"

    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(remote_content, canonical_url, "author", "name", None)
    )
    mock_repair_flow.coordinator.async_detect_risks_for_update = AsyncMock(return_value=[])

    concurrent_content = "blueprint:\n  name: Concurrently Modified During Diff\n"
    real_read_and_diff = BlueprintUpdateCoordinator._read_and_diff

    def _read_and_diff_with_concurrent_mutation(
        local_path: str, remote_text: str, source_url: str
    ) -> str:
        """Read diff and mutate file on disk concurrently."""
        diff = real_read_and_diff(local_path, remote_text, source_url)
        bp_file.write_text(concurrent_content, encoding="utf-8")
        return diff

    with patch(
        "custom_components.blueprints_updater.coordinator.BlueprintUpdateCoordinator._read_and_diff",
        side_effect=_read_and_diff_with_concurrent_mutation,
    ):
        result = await mock_repair_flow.async_step_change_url(
            {"url": "https://valid.url/test.yaml"}
        )

    assert result.get("type") == data_entry_flow.FlowResultType.FORM
    assert result.get("step_id") == "confirm_risks"
    assert mock_repair_flow._pending_precondition is not None
    assert mock_repair_flow._pending_precondition.content_hash == initial_hash
    assert mock_repair_flow._pending_precondition.content_hash != BlueprintFileStore._hash_file(
        str(bp_file)
    )

    with (
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as mock_delete,
        pytest.raises(HomeAssistantError, match="Local blueprint changed"),
    ):
        await mock_repair_flow.async_step_confirm_risks({"risk_action": RepairRiskAction.PROCEED})

    mock_delete.assert_not_called()
    assert bp_file.read_text(encoding="utf-8") == concurrent_content
    assert (
        mock_repair_flow.coordinator.data[mock_repair_flow.path]["source_url"]
        == original_source_url
    )


@pytest.mark.asyncio
async def test_change_url_capture_precondition_failure_aborts_flow(mock_repair_flow):
    """Test capture_precondition failure during change_url sets invalid_url."""
    content = "blueprint:\n  name: Updated Blueprint\n"
    canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow.coordinator.async_fetch_import_data = AsyncMock(
        return_value=(content, canonical_url, "author", "name", None)
    )

    with patch(
        "custom_components.blueprints_updater.file_store.BlueprintFileStore.capture_precondition",
        side_effect=OSError("Symlink or unreadable file"),
    ):
        result = await mock_repair_flow.async_step_change_url(
            {"url": "https://valid.url/test.yaml"}
        )

        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("step_id") == "change_url"
        assert result.get("errors") == {"url": "invalid_url"}
        assert mock_repair_flow._pending_precondition is None


@pytest.mark.asyncio
async def test_confirm_risks_proceed_without_precondition_rejected(mock_repair_flow):
    """Test PROCEED without a valid captured precondition returns to change_url."""
    mock_repair_flow._pending_content = "blueprint:\n  name: New\n"
    mock_repair_flow._pending_canonical_url = "https://canonical.url/test.yaml"
    mock_repair_flow._pending_precondition = None

    with patch.object(mock_repair_flow.coordinator, "async_install_blueprint") as mock_install:
        result = await mock_repair_flow.async_step_confirm_risks(
            {"risk_action": RepairRiskAction.PROCEED}
        )

        assert result.get("type") == data_entry_flow.FlowResultType.FORM
        assert result.get("step_id") == "change_url"
        assert result.get("errors") == {"url": "invalid_url"}
        mock_install.assert_not_called()
