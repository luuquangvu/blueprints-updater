"""Utility functions for Blueprints Updater."""

import asyncio
import inspect
import logging
import random
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, TypeVar

from .const import (
    CONF_MAX_BACKUPS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MAX_BACKUPS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    MAX_BACKUPS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_BACKUPS,
    MIN_UPDATE_INTERVAL,
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
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            sig = None

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            context = "unknown"
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


def get_config_int(
    config: Any,
    key: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Get an integer value from config entry options or data with clamping.

    Args:
        config: ConfigEntry, dict or None.
        key: Configuration key.
        default: Default value if not found or invalid.
        min_val: Optional minimum value for clamping.
        max_val: Optional maximum value for clamping.

    Returns:
        The coerced and clamped integer value.

    """
    if config is None:
        return default

    if hasattr(config, "options"):
        val = config.options.get(key, config.data.get(key, default))
    elif isinstance(config, dict):
        val = config.get(key, default)
    else:
        val = default

    try:
        res = int(str(val).strip())
    except (ValueError, TypeError):
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
