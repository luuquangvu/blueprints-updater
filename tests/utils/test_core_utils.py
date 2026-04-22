"""Tests for Blueprints Updater utilities."""

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from custom_components.blueprints_updater.const import CONF_MAX_BACKUPS, CONF_UPDATE_INTERVAL
from custom_components.blueprints_updater.utils import (
    get_config_int,
    get_max_backups,
    get_update_interval,
    retry_async,
)


@pytest.mark.asyncio
async def test_retry_async_success():
    """Test retry_async decorator when it succeeds immediately."""
    mock = MagicMock()

    async def mock_func() -> str:
        """Real async function to satisfy IDE, calling mock to track execution."""
        mock()
        return "success"

    @retry_async(3, (Exception,))
    async def decorated_func():
        """Mock decorated_func."""
        return await mock_func()

    result = await decorated_func()
    assert result == "success"
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_retry_async_retry_success():
    """Test retry_async decorator when it succeeds after some retries."""
    call_count = 0

    @retry_async(3, (ValueError,), base_delay=0.01)
    async def decorated_func():
        """Mock decorated_func."""
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("Failure")
        return "success"

    result = await decorated_func()
    assert result == "success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_async_failure():
    """Test retry_async decorator when it fails after all retries."""
    call_count = 0

    @retry_async(2, (ValueError,), base_delay=0.01)
    async def decorated_func():
        """Mock decorated_func."""
        nonlocal call_count
        call_count += 1
        raise ValueError(f"Failure {call_count}")

    with pytest.raises(ValueError, match="Failure 3"):
        await decorated_func()
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_async_specific_exceptions():
    """Test retry_async decorator with specific exceptions."""
    call_count = 0

    @retry_async(3, (ValueError,), base_delay=0.01)
    async def decorated_func():
        """Mock decorated_func."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("Retryable")
        raise TypeError("Not retryable")

    with pytest.raises(TypeError, match="Not retryable"):
        await decorated_func()
    assert call_count == 2


@pytest.mark.asyncio
async def test_retry_async_cancelled_error():
    """Test that retry_async does not catch CancelledError."""
    call_count = 0

    @retry_async(3, (Exception,), base_delay=0.01)
    async def decorated_func():
        """Mock decorated_func."""
        nonlocal call_count
        call_count += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await decorated_func()
    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_async_zero_retries():
    """Test retry_async with max_retries=0 performs one attempt and no retries."""
    call_count = 0

    @retry_async(0, (ValueError,), base_delay=0.01)
    async def decorated_func():
        """Mock decorated_func."""
        nonlocal call_count
        call_count += 1
        raise ValueError("Immediately fail")

    with pytest.raises(ValueError, match="Immediately fail"):
        await decorated_func()
    assert call_count == 1


def test_retry_async_invalid_args():
    """Test retry_async decorator with invalid arguments."""
    with pytest.raises(ValueError, match="max_retries must be greater than or equal to 0"):

        @retry_async(-1, (Exception,))
        async def mock_func_1():
            """Mock mock_func_1."""
            pass

    with pytest.raises(ValueError, match="base_delay must be greater than or equal to 0"):

        @retry_async(3, (Exception,), base_delay=-1.0)
        async def mock_func_2():
            """Mock mock_func_2."""
            pass

    with pytest.raises(ValueError, match="exceptions tuple must not be empty"):

        @retry_async(3, ())
        async def mock_func_3():
            """Mock mock_func_3."""
            pass

    with pytest.raises(TypeError, match="exceptions must be a tuple of Exception subclasses"):
        cast(Any, retry_async)(3, [Exception])

    with pytest.raises(TypeError, match="All items in exceptions must be subclasses of Exception"):
        cast(Any, retry_async)(3, (str,))

    with pytest.raises(TypeError, match="All items in exceptions must be subclasses of Exception"):
        cast(Any, retry_async)(3, (Exception, str))


def test_get_config_int():
    """Test get_config_int helper."""
    config = MagicMock()
    config.options = {"key": " 10 "}
    assert get_config_int(config, "key", 5) == 10
    assert get_config_int(config, "missing", 5) == 5
    assert get_config_int(None, "key", 5) == 5
    assert get_config_int(config, "key", 5, min_val=20) == 20
    assert get_config_int(config, "key", 5, max_val=5) == 5

    config.options = {"key": "invalid"}
    assert get_config_int(config, "key", 5) == 5

    config.options = {"key": "24.0"}
    assert get_config_int(config, "key", 5) == 24

    config.options = {"key": 24.0}
    assert get_config_int(config, "key", 5) == 24

    config.options = {"key": " 12.7 "}
    assert get_config_int(config, "key", 5) == 12


def test_get_update_interval():
    """Test get_update_interval helper."""
    config = MagicMock()

    config.options = {"update_interval": 48}
    assert get_update_interval(config) == 48

    config.options = {"update_interval": 0}
    assert get_update_interval(config) == 1

    config.options = {"update_interval": 1000}
    assert get_update_interval(config) == 720


def test_get_max_backups():
    """Test get_max_backups helper."""
    config = MagicMock()

    config.options = {"max_backups": 5}
    assert get_max_backups(config) == 5

    config.options = {"max_backups": 0}
    assert get_max_backups(config) == 1

    config.options = {"max_backups": 20}
    assert get_max_backups(config) == 10


@pytest.mark.asyncio
async def test_utils_behavior():
    """Test utils behavior."""
    assert get_config_int("NOT_A_DICT_OR_OBJ", "key", 10) == 10
    assert get_update_interval(None) == 24
    assert get_update_interval({CONF_UPDATE_INTERVAL: 12}) == 12

    entry = MagicMock()
    entry.options = {CONF_UPDATE_INTERVAL: 15}
    entry.data = {}
    assert get_update_interval(entry) == 15

    assert get_max_backups(None) == 3
    assert get_max_backups({CONF_MAX_BACKUPS: 5}) == 5

    mock_calls = 0

    async def mock_func(*args, **kwargs):
        nonlocal mock_calls
        mock_calls += 1
        raise RuntimeError("Fail")

    with pytest.raises(RuntimeError, match="Fail"):
        await retry_async(max_retries=2, base_delay=0, exceptions=(RuntimeError,))(mock_func)()
    assert mock_calls == 3
