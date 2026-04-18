"""Constants for the Pyscript Updater integration."""

import re

DOMAIN = "pyscript_updater"

CONF_PYSCRIPT_DIR = "pyscript_dir"
CONF_MANIFEST_FILE = "manifest_file"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_AUTO_UPDATE = "auto_update"
CONF_MAX_BACKUPS = "max_backups"
CONF_GITHUB_TOKEN = "github_token"
CONF_RELOAD_AFTER_UPDATE = "reload_after_update"

DEFAULT_PYSCRIPT_DIR = "/config/pyscript"
DEFAULT_MANIFEST_FILE = "_sources.txt"
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_MAX_BACKUPS = 3
DEFAULT_RELOAD_AFTER_UPDATE = True

MIN_UPDATE_INTERVAL = 1
MAX_UPDATE_INTERVAL_HOURS = 720
MIN_BACKUPS = 1
MAX_BACKUPS = 10

STORAGE_VERSION = 1
STORAGE_KEY_DATA = f"{DOMAIN}_data"

DOMAIN_GITHUB = "github.com"
DOMAIN_GITHUB_RAW = "raw.githubusercontent.com"
DOMAIN_GITHUB_API = "api.github.com"

RE_GITHUB_BLOB = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", re.IGNORECASE
)
RE_GITHUB_TREE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.+))?$", re.IGNORECASE
)
RE_GITHUB_RAW = re.compile(
    r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", re.IGNORECASE
)
RE_FILE_EXT = re.compile(
    r"\.(py|yaml|yml|json|txt|sh|js|lua|conf|cfg|ini|toml|md)(\?.*)?$", re.IGNORECASE
)

MAX_CONCURRENT_REQUESTS = 5
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 5
