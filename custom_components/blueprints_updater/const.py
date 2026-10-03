"""Constants for the Blueprints Updater integration."""

import re
from enum import StrEnum
from typing import Final

from homeassistant.components.automation.const import CONF_TRIGGER_VARIABLES
from homeassistant.const import CONF_MODE, CONF_VARIABLES
from homeassistant.helpers.script import CONF_MAX, CONF_MAX_EXCEEDED

DOMAIN = "blueprints_updater"
BLUEPRINTS_DATA_DIR = "blueprints"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_FILTER_MODE = "filter_mode"
CONF_SELECTED_BLUEPRINTS = "selected_blueprints"
CONF_AUTO_UPDATE = "auto_update"
CONF_MAX_BACKUPS = "max_backups"
CONF_VERIFY_ON_HA_UPDATE = "verify_on_ha_update"
EVENT_BLUEPRINTS_UPDATER_UPDATED = f"{DOMAIN}_updated"

# ASCII Unit Separator is not a Jinja2 syntax character. It separates a
# structured error key from its detail without colliding with template pipes.
ERROR_SEPARATOR = "\x1f"

DEFAULT_AUTO_UPDATE = False
DEFAULT_VERIFY_ON_HA_UPDATE = True
DEFAULT_MAX_BACKUPS = 3
MIN_BACKUPS = 1
MAX_BACKUPS = 10


class FilterMode(StrEnum):
    """Filter modes for blueprint update tracking."""

    ALL = "all"
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


class FunctionalDomain(StrEnum):
    """Home Assistant functional domains supported by blueprints."""

    AUTOMATION = "automation"
    SCRIPT = "script"
    TEMPLATE = "template"


URL_BLUEPRINT_DASHBOARD = "/config/blueprint/dashboard"

ALLOWED_RELOAD_DOMAINS = {
    FunctionalDomain.AUTOMATION,
    FunctionalDomain.SCRIPT,
    FunctionalDomain.TEMPLATE,
}

DEFAULT_UPDATE_INTERVAL_HOURS = 24
MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL_HOURS = 720

STORAGE_VERSION = 1
STORAGE_KEY_DATA = f"{DOMAIN}_data"
STORAGE_KEY_LAST_HA_VERSION = "last_ha_version"
METADATA_STORAGE_FIELDS = (
    "etag",
    "remote_hash",
    "source_url",
    "last_modified",
    "pinned",
    "pinned_reason",
    "pinned_at",
    "upstream_incompatible_hash",
    "dismissed_warning",
)
BLUEPRINT_ROUNDTRIP_INVARIANT_KEYS: Final[tuple[str, ...]] = (
    CONF_MODE,
    CONF_MAX,
    CONF_MAX_EXCEEDED,
    CONF_VARIABLES,
    CONF_TRIGGER_VARIABLES,
)

PLURAL_CONFIG_KEYS: Final[dict[str, str]] = {
    "trigger": "triggers",
    "condition": "conditions",
    "action": "actions",
}
"""Mapping of top-level singular configuration keys to their plural counterparts."""


class SourceDomain(StrEnum):
    """Domains for blueprint source providers."""

    GITHUB = "github.com"
    GITHUB_RAW = "raw.githubusercontent.com"
    GIST = "gist.github.com"
    GIST_RAW = "gist.githubusercontent.com"
    HA_FORUM = "community.home-assistant.io"
    GITLAB = "gitlab.com"
    CODEBERG = "codeberg.org"
    BITBUCKET = "bitbucket.org"


class PinReason(StrEnum):
    """Reasons why a blueprint is pinned."""

    HA_UPDATE_INCOMPATIBILITY = "ha_update_incompatibility"
    USER_APPLIED_AUTO_FIX = "user_applied_auto_fix"
    MANUAL = "manual"


URL_HA_DOCS_ACTIONS = "https://www.home-assistant.io/blog/2024/08/07/release-20248/"
URL_HA_DOCS_TEMPLATING_MATH = "https://www.home-assistant.io/template-functions/#math"
URL_HA_DOCS_TEMPLATING_NUMERIC = (
    "https://www.home-assistant.io/docs/templating/types/#converting-between-types"
)
URL_HA_DOCS_TARGETING = "https://www.home-assistant.io/docs/scripts/#performing-an-action-in-yaml"
URL_HA_DOCS_BLUEPRINT_DEFAULT = "https://www.home-assistant.io/docs/blueprint/"

URL_GITHUB_ISSUES_TEMPLATE = "https://github.com/{owner}/{repo}/issues"
URL_GIST_COMMENTS_TEMPLATE = "https://gist.github.com/{gist_id}#comments"
URL_HA_COMMUNITY_TOPIC_TEMPLATE = "https://community.home-assistant.io/t/{topic_id}"


RE_FORUM_TOPIC_ID = re.compile(r"/t/(?:[^/]+/)?(\d+)")
RE_FORUM_CODE_BLOCK = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL)
RE_URL_REDACTION = re.compile(r"https?://\S+", re.IGNORECASE)
RE_GIST_RAW = re.compile(r"/raw(/|$)")

MAX_CONCURRENT_REQUESTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 15
MAX_RETRIES = 5
RETRY_BACKOFF = 8
MIN_SEND_INTERVAL = 0.5
MAX_SEND_INTERVAL = 1.5

SPECIAL_USE_TLDS = {
    "local",
    "localhost",
    "test",
    "invalid",
    "example",
    "internal",
    "onion",
    "home.arpa",
}


class SourceProviderType(StrEnum):
    """Types of blueprint source providers."""

    GITHUB = "github"
    GIST = "gist"
    HA_FORUM = "ha_forum"
    GITLAB = "gitlab"
    CODEBERG = "codeberg"
    BITBUCKET = "bitbucket"
    GENERIC = "generic"


class IntegrationService(StrEnum):
    """Services provided by the integration."""

    RELOAD = "reload"
    RESTORE_BLUEPRINT = "restore_blueprint"
    UPDATE_ALL = "update_all"
    IMPORT_BLUEPRINT = "import_blueprint"
    CHECK_COMPATIBILITY = "check_compatibility"


class BlueprintRiskType(StrEnum):
    """Risk types for breaking change detection."""

    NEW_MANDATORY = "new_mandatory"
    MISSING_INPUT = "missing_input"
    REMOVED_INPUT = "removed_input"
    SELECTOR_MISMATCH = "selector_mismatch"
    SELECTOR_CONFIG_CHANGED = "selector_config_changed"
    COMPATIBILITY = "compatibility_risk"
    VALIDATION_FAILED = "validation_failed_blueprint"
    SYSTEM_ERROR = "system_error"


class BlueprintBlockingReason(StrEnum):
    """Reasons why an update or auto-update is blocked."""

    BREAKING_CHANGE = "auto_update_blocked_by_breaking_change"
    SYSTEM_ERROR = "auto_update_blocked_by_system_error"


RISK_TYPE_TRANSLATIONS = {
    BlueprintRiskType.NEW_MANDATORY: "risk_new_mandatory",
    BlueprintRiskType.MISSING_INPUT: "risk_missing_input",
    BlueprintRiskType.REMOVED_INPUT: "risk_removed_input",
    BlueprintRiskType.SELECTOR_MISMATCH: "risk_selector_mismatch",
    BlueprintRiskType.SELECTOR_CONFIG_CHANGED: "risk_selector_config_changed",
    BlueprintRiskType.COMPATIBILITY: "risk_compatibility",
    BlueprintRiskType.VALIDATION_FAILED: "risk_validation_failed_blueprint",
    BlueprintRiskType.SYSTEM_ERROR: "risk_system_error",
}

"""List of MIME types considered valid for YAML blueprint files.

Includes text/plain for compatibility with raw text providers like Gists.
"""
ALLOWED_YAML_MIME_TYPES = [
    "application/x-yaml",
    "application/yaml",
    "text/plain",
    "text/x-yaml",
    "text/yaml",
]


class RepairIssueType(StrEnum):
    """Types of repair issues raised by Blueprints Updater."""

    WITHDRAWN_BLUEPRINT = "withdrawn_blueprint"
    INCOMPATIBLE_BLUEPRINT = "incompatible_blueprint"


class RepairIncompatibleAction(StrEnum):
    """Actions available in the incompatible blueprint repair menu."""

    AUTO_FIX = "auto_fix"
    ACKNOWLEDGE = "acknowledge"
    CHANGE_URL = "change_url"
    UNPIN = "unpin"


class IncompatibilitySeverity(StrEnum):
    """Severity classification for blueprint incompatibility."""

    BREAKING = "breaking"
    DEPRECATION = "deprecation"


class RepairForkAction(StrEnum):
    """Actions available when confirming a fork switch."""

    PROCEED = "proceed"
    AUTO_FIX = "auto_fix"
    DIFFERENT_URL = "different_url"


class RepairAction(StrEnum):
    """Actions available in the withdrawn blueprint repair menu."""

    CHANGE_URL = "change_url"
    STOP_TRACKING = "stop_tracking"
    DELETE_BLUEPRINT = "delete_blueprint"


class RepairRiskAction(StrEnum):
    """Actions available when reviewing compatibility risks."""

    PROCEED = "proceed"
    DIFFERENT_URL = "different_url"
    STOP_TRACKING = "stop_tracking"


class RepairError(StrEnum):
    """Validation and execution error keys for repair flows."""

    MISSING_URL = "missing_url"
    INVALID_URL = "invalid_url"
    CONFIRMATION_REQUIRED = "confirmation_required"
    USAGE_DISCOVERY_FAILED = "usage_discovery_failed"
