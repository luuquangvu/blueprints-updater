"""Utility helpers for Pyscript Updater."""

from __future__ import annotations

from typing import Any

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


def _get_option(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    if hasattr(config, "options"):
        return config.options.get(key, config.data.get(key, default))
    if isinstance(config, dict):
        return config.get(key, default)
    return default


def get_config_int(
    config: Any,
    key: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Get an int option clamped to [min_val, max_val]."""
    val = _get_option(config, key, default)
    try:
        result = int(float(str(val).strip()))
    except (ValueError, TypeError, OverflowError):
        return default
    if min_val is not None:
        result = max(min_val, result)
    if max_val is not None:
        result = min(max_val, result)
    return result


def get_update_interval(config: Any) -> int:
    """Return update interval (hours), clamped."""
    return get_config_int(
        config,
        CONF_UPDATE_INTERVAL,
        DEFAULT_UPDATE_INTERVAL_HOURS,
        min_val=MIN_UPDATE_INTERVAL,
        max_val=MAX_UPDATE_INTERVAL_HOURS,
    )


def get_max_backups(config: Any) -> int:
    """Return max backups, clamped."""
    return get_config_int(
        config,
        CONF_MAX_BACKUPS,
        DEFAULT_MAX_BACKUPS,
        min_val=MIN_BACKUPS,
        max_val=MAX_BACKUPS,
    )


def get_option(config: Any, key: str, default: Any) -> Any:
    """Public wrapper to read an option from a ConfigEntry or dict."""
    return _get_option(config, key, default)
