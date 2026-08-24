"""Utility functions for Blueprints Updater."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import os
import random
import textwrap
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import ParamSpec, TypeVar
from urllib.parse import quote

import httpx
from homeassistant.components.automation import automations_with_blueprint
from homeassistant.components.script import scripts_with_blueprint
from homeassistant.components.template.helpers import templates_with_blueprint
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ALLOWED_RELOAD_DOMAINS,
    BLUEPRINTS_DATA_DIR,
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MAX_BACKUPS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    ERROR_SEPARATOR,
    MAX_BACKUPS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_BACKUPS,
    MIN_UPDATE_INTERVAL,
    RE_URL_REDACTION,
    URL_BLUEPRINT_DASHBOARD,
    FilterMode,
    FunctionalDomain,
)
from .exceptions import BlueprintFetchPolicyError
from .providers import registry

_LOGGER = logging.getLogger(__name__)

P = ParamSpec("P")
_T = TypeVar("_T")


def retry_async(
    max_retries: int,
    exceptions: tuple[type[Exception], ...],
    base_delay: float = 5.0,
    exponential: bool = True,
    jitter: bool = True,
) -> Callable[
    [Callable[P, Coroutine[object, object, _T]]],
    Callable[P, Coroutine[object, object, _T]],
]:
    """Decorator to retry an async function with exponential backoff and jitter.

    Args:
        max_retries: The maximum number of retry attempts.
        exceptions: A tuple of exception classes to catch and retry on.
        base_delay: The initial delay before retrying.
        exponential: Whether to use exponential backoff.
        jitter: Whether to add random jitter to the delay.

    Returns:
        Decorated async function.
    """
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise TypeError("max_retries must be an integer")
    if max_retries < 0:
        raise ValueError("max_retries must be greater than or equal to 0")
    if base_delay < 0:
        raise ValueError("base_delay must be greater than or equal to 0")
    if not exceptions:
        raise ValueError("exceptions tuple must not be empty")
    for exc in exceptions:
        if not (inspect.isclass(exc) and issubclass(exc, Exception)):
            raise TypeError(f"All items in exceptions must be subclasses of Exception, got {exc}")

    def decorator(
        func: Callable[P, Coroutine[object, object, _T]],
    ) -> Callable[P, Coroutine[object, object, _T]]:
        """Decorator for retry_async."""
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError, AttributeError):
            sig = None

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> _T:
            """Wrapper for retry_async."""
            if sig:
                try:
                    bound_args = sig.bind(*args, **kwargs)
                    context = bound_args.arguments.get("url", "unknown")
                except (ValueError, TypeError, AttributeError):
                    context = getattr(func, "__name__", "unknown")
            else:
                context = getattr(func, "__name__", "unknown")
            safe_context = redact_url(str(context))

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except exceptions as err:
                    if (
                        isinstance(err, httpx.HTTPStatusError)
                        and err.response.is_client_error
                        and err.response.status_code
                        not in (
                            httpx.codes.TOO_MANY_REQUESTS,
                            httpx.codes.REQUEST_TIMEOUT,
                            httpx.codes.TOO_EARLY,
                        )
                    ):
                        _LOGGER.debug(
                            "Non-retryable HTTP status code %d for %s; failing fast",
                            err.response.status_code,
                            safe_context,
                        )
                        raise

                    safe_error = sanitize_error_detail(str(err))
                    if attempt >= max_retries:
                        _LOGGER.error(
                            "Could not update from %s after %d attempts: %s",
                            safe_context,
                            attempt + 1,
                            safe_error,
                        )
                        raise

                    wait = (base_delay * (2**attempt) if exponential else base_delay) + (
                        random.uniform(0, base_delay) if jitter else 0
                    )
                    _LOGGER.debug(
                        "Retrying lookup for %s due to %s (Retry %d/%d, wait %.2fs)",
                        safe_context,
                        safe_error,
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)

            raise RuntimeError("Unreachable")

        return wrapper

    return decorator


def get_config_value(config: object, key: str, default: object) -> object:
    """Get a value from config entry options strictly (no data fallback).

    Args:
        config: ConfigEntry, dict or None.
        key: Configuration key.
        default: Default value if not found.

    Returns:
        The configuration value.

    """
    if config is None:
        return default

    if hasattr(config, "options"):
        options = getattr(config, "options", None)
        get_fn = getattr(options, "get", None)
        if callable(get_fn):
            val = get_fn(key, default)
            return default if val is None else val
    get_cfg_fn = getattr(config, "get", None)
    if callable(get_cfg_fn):
        val = get_cfg_fn(key, default)
        return default if val is None else val

    return default


def get_config_bool(config: object, key: str, default: bool) -> bool:
    """Get a boolean value from config entry options strictly (no data fallback).

    Args:
        config: ConfigEntry, dict or None.
        key: Configuration key.
        default: Default value if not found.

    Returns:
        The boolean value.

    """
    val = get_config_value(config, key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "on", "1")
    return bool(val)


def get_config_str(config: object, key: str, default: str) -> str:
    """Get a string value from config entry options strictly (no data fallback).

    Args:
        config: ConfigEntry, dict or None.
        key: Configuration key.
        default: Default value if not found.

    Returns:
        The string value.

    """
    return str(get_config_value(config, key, default))


def get_config_int(
    config: object,
    key: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Get an integer value from config entry options strictly (no data fallback).

    Args:
        config: ConfigEntry, dict or None.
        key: Configuration key.
        default: Default value if not found or invalid.
        min_val: Optional minimum value for clamping.
        max_val: Optional maximum value for clamping.

    Returns:
        The coerced and clamped integer value.

    """
    val = get_config_value(config, key, default)

    try:
        res = int(float(str(val).strip()))
    except (ValueError, TypeError, OverflowError):
        return default

    if min_val is not None:
        res = max(min_val, res)
    if max_val is not None:
        res = min(max_val, res)
    return res


def get_update_interval(config: object) -> int:
    """Get the normalized update interval in hours.

    Args:
        config: ConfigEntry, dict or None.

    Returns:
        The normalized interval.

    """
    return get_config_int(
        config,
        CONF_UPDATE_INTERVAL,
        DEFAULT_UPDATE_INTERVAL_HOURS,
        min_val=MIN_UPDATE_INTERVAL,
        max_val=MAX_UPDATE_INTERVAL_HOURS,
    )


def get_max_backups(config: object) -> int:
    """Get the normalized maximum number of backups.

    Args:
        config: ConfigEntry, dict or None.

    Returns:
        The normalized number of backups.

    """
    return get_config_int(
        config,
        CONF_MAX_BACKUPS,
        DEFAULT_MAX_BACKUPS,
        min_val=MIN_BACKUPS,
        max_val=MAX_BACKUPS,
    )


def normalize_url(url: str) -> str:
    """Convert known source URLs to their raw or API endpoints.

    Args:
        url: The user-provided source URL.

    Returns:
        The normalized URL for direct content fetching.

    """
    if provider := registry.get_provider(url):
        return provider.normalize_url(url)
    return url


def normalize_domain(domain: object) -> FunctionalDomain:
    """Normalize and validate the blueprint domain, defaulting to automation.

    Args:
        domain: The domain to normalize.

    Returns:
        The normalized FunctionalDomain enum.

    """
    if isinstance(domain, FunctionalDomain):
        return domain

    if isinstance(domain, str):
        norm_domain = domain.strip().lower()
        for fd in FunctionalDomain:
            if norm_domain == fd.value:
                return fd

    if domain and str(domain).strip():
        _LOGGER.warning(
            "Unsupported or unknown blueprint domain '%s' encountered; "
            "falling back to 'automation'. Supported: %s",
            domain,
            ", ".join(ALLOWED_RELOAD_DOMAINS),
        )

    return FunctionalDomain.AUTOMATION


def get_validated_filter_mode(filter_mode: object) -> FilterMode:
    """Normalize and validate filter mode.

    Args:
        filter_mode: The filter mode to validate.

    Returns:
        A valid FilterMode enum, falling back to FilterMode.ALL.

    """
    if isinstance(filter_mode, FilterMode):
        return filter_mode

    if not isinstance(filter_mode, str):
        if filter_mode is not None:
            _LOGGER.warning(
                "Invalid filter mode type '%s'; falling back to all", type(filter_mode).__name__
            )
        return FilterMode.ALL

    normalized_mode = filter_mode.strip().lower()
    for mode in FilterMode:
        if normalized_mode == mode.value:
            return mode

    _LOGGER.warning("Invalid filter mode '%s' in config; falling back to all", filter_mode)
    return FilterMode.ALL


def get_blueprint_usage_entities(
    hass: HomeAssistant,
    domain: str | FunctionalDomain | None,
    blueprint_id: str,
) -> list[str] | None:
    """Return all entity IDs currently using the specified blueprint.

    Args:
        hass: Home Assistant instance.
        domain: FunctionalDomain (automation, script, template) or None for all.
        blueprint_id: Blueprint identifier (e.g. author/name.yaml).

    Returns:
        List of unique entity IDs using the blueprint.
        None if one or more domain lookups failed.

    """
    result: list[str] = []
    lookup_failed = False
    norm_domain: FunctionalDomain | None
    if isinstance(domain, FunctionalDomain):
        norm_domain = domain
    elif domain:
        norm_domain = normalize_domain(domain)
    else:
        norm_domain = None

    domain_fetchers = (
        (FunctionalDomain.AUTOMATION, automations_with_blueprint),
        (FunctionalDomain.SCRIPT, scripts_with_blueprint),
        (FunctionalDomain.TEMPLATE, templates_with_blueprint),
    )

    for target_domain, fetcher in domain_fetchers:
        if norm_domain is None or norm_domain == target_domain:
            try:
                result.extend(fetcher(hass, blueprint_id))
            except HomeAssistantError as err:
                lookup_failed = True
                _LOGGER.warning(
                    "Could not calculate %s usage for blueprint %s: %s",
                    target_domain,
                    blueprint_id,
                    err,
                )

    return None if lookup_failed else list(dict.fromkeys(result))


def get_validated_selected_blueprints(selected: object) -> list[str]:
    """Validate and coerce selected blueprints into a list of strings.

    Args:
        selected: The selection value to validate.

    Returns:
        A valid list of blueprint paths.

    """
    if selected is None:
        return []

    if isinstance(selected, str):
        stripped = selected.strip()
        return [stripped] if stripped else []

    if isinstance(selected, (list, tuple)):
        return [str(item).strip() for item in selected if item and str(item).strip()]

    if isinstance(selected, dict):
        _LOGGER.error(
            "Invalid type for selected blueprints: mapping (%s) provided; "
            "expected string or sequence of strings. Ignoring value.",
            type(selected).__name__,
        )
        return []

    _LOGGER.error(
        "Invalid type for selected blueprints: %s; expected string or sequence of strings. "
        "Ignoring value.",
        type(selected).__name__,
    )
    return []


def should_include_blueprint(
    relative_path: str,
    filter_mode: FilterMode,
    selected_set: set[str],
) -> bool:
    """Check if a blueprint should be included based on filtering rules."""
    if filter_mode == FilterMode.BLACKLIST:
        return relative_path not in selected_set

    if filter_mode == FilterMode.WHITELIST:
        return relative_path in selected_set

    return True


def read_local_file(full_path: str) -> str | None:
    """Read a local UTF-8 file if it exists and is a regular file.

    Args:
        full_path: Absolute path to the file.

    Returns:
        The file content string, or None if the file does not exist or is not a file.

    """
    if not os.path.isfile(full_path):
        return None
    with open(full_path, encoding="utf-8") as file:
        return file.read()


def redact_url(url: str | None) -> str:
    """Redact sensitive parts of a URL (credentials, query, fragment)."""
    if not url:
        return "None"
    try:
        parsed = httpx.URL(url)
        return str(parsed.copy_with(username=None, password=None, query=None, fragment=None))
    except Exception:
        return "[REDACTED/INVALID URL]"


def sanitize_error_detail(detail: str, max_length: int = 120) -> str:
    """Sanitize error detail to avoid delimiter clashes and overly long messages.

    Args:
        detail: The raw error message string.
        max_length: Maximum allowed length for the sanitized string.

    Returns:
        The sanitized and potentially truncated error string.

    """
    cleaned = RE_URL_REDACTION.sub(lambda m: redact_url(m.group(0)), detail)
    cleaned = cleaned.replace(ERROR_SEPARATOR, "/")
    return textwrap.shorten(cleaned, width=max_length, placeholder="...")


def format_error_message(error_key: str, detail: object) -> str:
    """Format a structured error with a sanitized detail."""
    return f"{error_key}{ERROR_SEPARATOR}{sanitize_error_detail(str(detail))}"


def split_error_message(error: str) -> tuple[str, str] | None:
    """Split a structured error into its key and detail."""
    if ERROR_SEPARATOR not in error:
        return None
    key, detail = error.split(ERROR_SEPARATOR, 1)
    return key, detail


def verify_https_enforcement(response: httpx.Response, original_url: str) -> None:
    """Verify that the response URL uses HTTPS scheme.

    Raises BlueprintFetchPolicyError if the scheme is not https.
    """
    if response.url.scheme != "https":
        _LOGGER.error(
            "Blocking unsafe final URL (non-HTTPS) for %s: %s",
            redact_url(original_url),
            response.url.scheme,
        )
        raise BlueprintFetchPolicyError(
            f"Security violation: Final destination for {redact_url(original_url)} "
            f"must be HTTPS (got {response.url.scheme})"
        )


def get_relative_path(hass: HomeAssistant, path: str) -> str:
    """Calculate normalized relative path from blueprints root.

    This ensures that paths are always forward-slash separated even on Windows,
    providing consistency across the integration.

    Args:
        hass: HomeAssistant instance.
        path: Absolute path to the blueprint.

    Returns:
        The normalized relative path string.

    """
    root = hass.config.path(BLUEPRINTS_DATA_DIR)
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)

    try:
        common = os.path.commonpath([real_path, real_root])
    except (ValueError, OSError) as err:
        raise ValueError(f"Invalid or unsafe path: {path}") from err

    if common != real_root:
        raise ValueError(f"Path escapes blueprints root: {path}")

    return os.path.relpath(real_path, real_root).replace("\\", "/")


def get_blueprint_relative_path(hass: HomeAssistant, path: str) -> str | None:
    """Get a relative path for a blueprint with centralized error handling.

    This helper wraps get_relative_path to provide a consistent way of
    handling invalid or unsafe paths across the integration.

    Args:
        hass: HomeAssistant instance.
        path: Absolute path to the blueprint.

    Returns:
        The relative path string if valid, None if the path is invalid or unsafe.

    """
    try:
        return get_relative_path(hass, path)
    except (ValueError, TypeError, OSError) as err:
        _LOGGER.debug("Skipping invalid blueprint path %s: %s", path, err)
        return None


def is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is safe (public).

    Args:
        ip: The IP address to check.

    Returns:
        True if the IP is public and safe.

    """
    return ip.is_global


def format_diff_block(diff_text: str | None, diff_title: str) -> str:
    """Format git diff with localized title, adaptive markdown fences, and collapsible details.

    Args:
        diff_text: Raw unified diff string, or None.
        diff_title: Localized summary title.

    Returns:
        Formatted Markdown string with collapsible details, or empty string.

    """
    if not isinstance(diff_text, str) or not diff_text.strip():
        return ""
    diff_stripped = diff_text.strip()
    fence = "```"
    while fence in diff_stripped:
        fence += "`"
    return (
        f"\n\n<details>\n<summary>{diff_title}</summary>\n\n"
        f"{fence}diff\n{diff_stripped}\n{fence}\n</details>"
    )


def get_blueprint_dashboard_url(domain: FunctionalDomain, blueprint_id: str) -> str:
    """Get the URL to the dashboard for the given domain and blueprint.

    Args:
        domain: Functional domain of the blueprint (automation, script, template).
        blueprint_id: Identifier of the blueprint.

    Returns:
        The dashboard URL for inspecting dependent entities.

    """
    if domain == FunctionalDomain.TEMPLATE:
        return URL_BLUEPRINT_DASHBOARD
    encoded_id = quote(blueprint_id, safe="")
    return f"/config/{domain.value}/dashboard?blueprint={encoded_id}"
