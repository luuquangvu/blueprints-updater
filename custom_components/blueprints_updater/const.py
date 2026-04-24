"""Constants for the Blueprints Updater integration."""

import re
from enum import StrEnum

DOMAIN = "blueprints_updater"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_FILTER_MODE = "filter_mode"
CONF_SELECTED_BLUEPRINTS = "selected_blueprints"
CONF_AUTO_UPDATE = "auto_update"
CONF_MAX_BACKUPS = "max_backups"
CONF_USE_CDN = "use_cdn"

DEFAULT_AUTO_UPDATE = False
DEFAULT_USE_CDN = True
DEFAULT_MAX_BACKUPS = 3
MIN_BACKUPS = 1
MAX_BACKUPS = 10

FILTER_MODE_ALL = "all"
FILTER_MODE_WHITELIST = "whitelist"
FILTER_MODE_BLACKLIST = "blacklist"
ALLOWED_RELOAD_DOMAINS = {"automation", "script", "template"}

DEFAULT_UPDATE_INTERVAL_HOURS = 24
MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL_HOURS = 720

STORAGE_VERSION = 1
STORAGE_KEY_DATA = f"{DOMAIN}_data"

DOMAIN_GITHUB = "github.com"
DOMAIN_GITHUB_RAW = "raw.githubusercontent.com"
DOMAIN_GIST = "gist.github.com"
DOMAIN_HA_FORUM = "community.home-assistant.io"
DOMAIN_JSDELIVR = "cdn.jsdelivr.net"

RE_FORUM_TOPIC_ID = re.compile(r"/t/(?:[^/]+/)?(\d+)")
RE_FORUM_CODE_BLOCK = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL)
RE_URL_REDACTION = re.compile(r"https?://\S+", re.IGNORECASE)
RE_GIST_RAW = re.compile(r"/raw(/|$)")

MAX_CONCURRENT_REQUESTS = 5
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
}


class IntegrationService(StrEnum):
    """Services provided by the integration."""

    RELOAD = "reload"
    RESTORE_BLUEPRINT = "restore_blueprint"
    UPDATE_ALL = "update_all"


class BlueprintRiskType(StrEnum):
    """Risk types for breaking change detection."""

    LEGACY = "legacy_risk"
    NEW_MANDATORY = "new_mandatory"
    MISSING_INPUT = "missing_input"
    REMOVED_INPUT = "removed_input"
    SELECTOR_MISMATCH = "selector_mismatch"
    COMPATIBILITY = "compatibility_risk"
    VALIDATION_FAILED = "validation_failed_blueprint"
    SYSTEM_ERROR = "system_error"


class BlueprintBlockingReason(StrEnum):
    """Reasons why an update or auto-update is blocked."""

    BREAKING_CHANGE = "auto_update_blocked_by_breaking_change"
    SYSTEM_ERROR = "auto_update_blocked_by_system_error"


RISK_TYPE_TRANSLATIONS = {
    BlueprintRiskType.LEGACY: "risk_legacy",
    BlueprintRiskType.NEW_MANDATORY: "risk_new_mandatory",
    BlueprintRiskType.MISSING_INPUT: "risk_missing_input",
    BlueprintRiskType.REMOVED_INPUT: "risk_removed_input",
    BlueprintRiskType.SELECTOR_MISMATCH: "risk_selector_mismatch",
    BlueprintRiskType.COMPATIBILITY: "risk_compatibility",
    BlueprintRiskType.VALIDATION_FAILED: "risk_validation_failed_blueprint",
    BlueprintRiskType.SYSTEM_ERROR: "risk_system_error",
}
