"""Utility functions for Blueprints Updater."""

import asyncio
import inspect
import logging
import os
import random
import textwrap
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

import httpx

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import (
    BLUEPRINTS_DATA_DIR,
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MAX_BACKUPS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    MAX_BACKUPS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_BACKUPS,
    MIN_UPDATE_INTERVAL,
    RE_URL_REDACTION,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")
AsyncFunc = Callable[..., Coroutine[Any, Any, _T]]


def retry_async(
    max_retries: int,
    exceptions: tuple[type[Exception], ...],
    base_delay: float = 5.0,
    exponential: bool = True,
    jitter: bool = True,
) -> Callable[[AsyncFunc[_T]], AsyncFunc[_T]]:
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
    if not isinstance(exceptions, tuple):
        raise TypeError("exceptions must be a tuple of Exception subclasses")
    if not exceptions:
        raise ValueError("exceptions tuple must not be empty")
    for exc in exceptions:
        if not (inspect.isclass(exc) and issubclass(exc, Exception)):
            raise TypeError(f"All items in exceptions must be subclasses of Exception, got {exc}")

    def decorator(func: AsyncFunc[_T]) -> AsyncFunc[_T]:
        """Decorator for retry_async."""
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            sig = None

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            """Wrapper for retry_async."""
            if sig:
                try:
                    bound_args = sig.bind(*args, **kwargs)
                    context = bound_args.arguments.get("url", "unknown")
                except (ValueError, TypeError):
                    context = getattr(func, "__name__", "unknown")
            else:
                context = getattr(func, "__name__", "unknown")

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except exceptions as err:
                    if attempt >= max_retries:
                        _LOGGER.error(
                            "Could not update from %s after %d attempts: %s",
                            context,
                            attempt + 1,
                            err,
                        )
                        raise

                    wait = (base_delay * (2**attempt) if exponential else base_delay) + (
                        random.uniform(0, base_delay) if jitter else 0
                    )
                    _LOGGER.debug(
                        "Retrying lookup for %s due to %s (Retry %d/%d, wait %.2fs)",
                        context,
                        err,
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)

            raise RuntimeError("Unreachable")

        return wrapper

    return decorator


def get_config_value[T](config: Any, key: str, default: T) -> T:
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
        val = config.options.get(key, default)
    elif isinstance(config, dict):
        val = config.get(key, default)
    else:
        val = default

    return cast(T, val)


def get_config_bool(config: Any, key: str, default: bool) -> bool:
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


def get_config_str(config: Any, key: str, default: str) -> str:
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
    config: Any,
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


def get_update_interval(config: Any) -> int:
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


def get_max_backups(config: Any) -> int:
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
    cleaned = cleaned.replace("|", "/")
    return textwrap.shorten(cleaned, width=max_length, placeholder="...")


def verify_https_enforcement(response: httpx.Response, original_url: str) -> None:
    """Verify that the response URL uses HTTPS scheme.

    Raises httpx.HTTPError if the scheme is not https.
    """
    if response.url.scheme != "https":
        _LOGGER.error(
            "Blocking unsafe final URL (non-HTTPS) for %s: %s",
            redact_url(original_url),
            response.url.scheme,
        )
        raise httpx.HTTPError(
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


def get_blueprint_rel_path(hass: HomeAssistant, path: str) -> str | None:
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
