"""Constants for the Blueprints Updater integration."""

import re

DOMAIN = "blueprints_updater"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_FILTER_MODE = "filter_mode"
CONF_SELECTED_BLUEPRINTS = "selected_blueprints"
CONF_AUTO_UPDATE = "auto_update"
CONF_MAX_BACKUPS = "max_backups"

DEFAULT_MAX_BACKUPS = 3

FILTER_MODE_ALL = "all"
FILTER_MODE_WHITELIST = "whitelist"
FILTER_MODE_BLACKLIST = "blacklist"

DEFAULT_UPDATE_INTERVAL_HOURS = 24

STORAGE_VERSION = 1
STORAGE_KEY_DATA = f"{DOMAIN}_data"

DOMAIN_GITHUB = "github.com"
DOMAIN_GITHUB_RAW = "raw.githubusercontent.com"
DOMAIN_GIST = "gist.github.com"
DOMAIN_HA_FORUM = "community.home-assistant.io"

RE_GITHUB_BLOB = re.compile(r"/blob/", re.IGNORECASE)
RE_GIST_RAW = re.compile(r"/raw/?$", re.IGNORECASE)
RE_FORUM_TOPIC_ID = re.compile(r"/t/(?:[^/]+/)?(\d+)")
RE_FORUM_CODE_BLOCK = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL)
RE_BLUEPRINT_KEY = re.compile(r"^(blueprint:.*)$", re.MULTILINE)
RE_SOURCE_URL_LINE = re.compile(r"^\s*source_url:\s*['\"]?(.*?)['\"]?\s*$", re.MULTILINE)

MAX_CONCURRENT_REQUESTS = 5
REQUEST_TIMEOUT = 15
MAX_RETRIES = 4
RETRY_BACKOFF = 8
MIN_SEND_INTERVAL = 0.5
MAX_SEND_INTERVAL = 1.5
