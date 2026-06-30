"""Tests for Blueprints Updater utilities."""

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest

from custom_components.blueprints_updater.const import CONF_MAX_BACKUPS, CONF_UPDATE_INTERVAL
from custom_components.blueprints_updater.utils import (
    get_config_bool,
    get_config_int,
    get_config_str,
    get_config_value,
    get_max_backups,
    get_relative_path,
    get_update_interval,
    normalize_url,
    redact_url,
    retry_async,
    sanitize_error_detail,
    should_include_blueprint,
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

    with pytest.raises(ValueError, match="base_delay must be greater than or equal to 0"):

        @retry_async(3, (Exception,), base_delay=-1.0)
        async def mock_func_2():
            """Mock mock_func_2."""

    with pytest.raises(ValueError, match="exceptions tuple must not be empty"):

        @retry_async(3, ())
        async def mock_func_3():
            """Mock mock_func_3."""

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


def test_get_config_bool():
    """Test get_config_bool helper."""
    config = MagicMock()
    config.options = {"key": True}
    assert get_config_bool(config, "key", False) is True
    assert get_config_bool(config, "missing", False) is False
    assert get_config_bool(None, "key", True) is True

    config.options = {"key": 0}
    assert get_config_bool(config, "key", True) is False

    assert get_config_bool({"key": "anything"}, "key", False) is False

    assert get_config_str({"key": 123}, "key", "default") == "123"


def test_get_config_bool_string_handling():
    """Test get_config_bool handles string boolean values correctly."""
    assert get_config_bool({"key": "true"}, "key", False) is True
    assert get_config_bool({"key": "TRUE"}, "key", False) is True
    assert get_config_bool({"key": "yes"}, "key", False) is True
    assert get_config_bool({"key": "on"}, "key", False) is True
    assert get_config_bool({"key": "1"}, "key", False) is True
    assert get_config_bool({"key": "false"}, "key", True) is False
    assert get_config_bool({"key": "0"}, "key", True) is False
    assert get_config_bool({"key": "anything else"}, "key", True) is False


def test_get_config_value():
    """Test get_config_value helper."""
    config = MagicMock()
    config.options = {"key": [1, 2, 3]}
    assert get_config_value(config, "key", []) == [1, 2, 3]
    assert get_config_value(config, "missing", ["default"]) == ["default"]
    assert get_config_value(None, "key", ["none"]) == ["none"]

    assert get_config_value({"key": {"a": 1}}, "key", {}) == {"a": 1}

    config_with_both = MagicMock()
    config_with_both.options = {"key": "from_options"}
    config_with_both.data = {"key": "from_data"}
    assert get_config_value(config_with_both, "key", "default") == "from_options"


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
        """Mock failure function."""
        nonlocal mock_calls
        mock_calls += 1
        raise RuntimeError("Fail")

    with pytest.raises(RuntimeError, match="Fail"):
        await retry_async(max_retries=2, base_delay=0, exceptions=(RuntimeError,))(mock_func)()
    assert mock_calls == 3


def test_redact_url():
    """Test redact_url utility."""
    assert redact_url(None) == "None"
    assert redact_url("") == "None"
    assert redact_url("invalid-url") == "invalid-url"
    assert redact_url("https://user:pass@github.com/path") == "https://github.com/path"
    assert redact_url("https://github.com/path?token=123") == "https://github.com/path"
    assert redact_url("https://github.com/path#fragment") == "https://github.com/path"
    assert redact_url("https://u:p@host.com/p?q=1#f") == "https://host.com/p"


def test_sanitize_error_detail():
    """Test sanitize_error_detail utility."""
    assert sanitize_error_detail("normal error") == "normal error"
    assert sanitize_error_detail("error | with | pipe") == "error / with / pipe"
    assert (
        sanitize_error_detail("failed at https://u:p@github.com/p?q=1")
        == "failed at https://github.com/p"
    )
    long_msg = "a" * 200
    sanitized = sanitize_error_detail(long_msg, max_length=50)
    assert len(sanitized) <= 50
    assert sanitized.endswith("...")


def test_retry_async_rejects_bool_max_retries():
    """Verify retry_async rejects bool values instead of accepting them as integers."""
    with pytest.raises(TypeError, match="max_retries must be an integer"):
        retry_async(True, (Exception,))


def test_source_url_helpers_return_safe_defaults_for_unknown_sources():
    """Verify URL helpers keep unknown malformed sources inert."""
    assert normalize_url("not-a-url") == "not-a-url"


def test_should_include_blueprint_filter_modes():
    """Verify whitelist and blacklist filtering make opposite inclusion decisions."""
    selected = {"automation/blocked.yaml"}

    assert not should_include_blueprint("automation/blocked.yaml", "blacklist", selected)
    assert should_include_blueprint("automation/allowed.yaml", "blacklist", selected)
    assert should_include_blueprint("automation/blocked.yaml", "whitelist", selected)
    assert not should_include_blueprint("automation/allowed.yaml", "whitelist", selected)
    assert should_include_blueprint("automation/allowed.yaml", "all", selected)


def test_redact_url_rejects_non_url_objects():
    """Verify redaction falls back safely for values httpx cannot parse."""
    assert redact_url(cast(str, object())) == "[REDACTED/INVALID URL]"


def test_get_relative_path_reports_commonpath_failures(hass, monkeypatch):
    """Verify invalid platform path comparisons are reported as unsafe paths."""
    hass.config.path.return_value = "/config/blueprints"

    def raise_commonpath_error(paths):
        """Simulate os.path.commonpath failing on incompatible paths."""
        raise ValueError(f"bad paths: {paths}")

    monkeypatch.setattr(
        "custom_components.blueprints_updater.utils.os.path.commonpath",
        raise_commonpath_error,
    )

    with pytest.raises(ValueError, match="Invalid or unsafe path"):
        get_relative_path(hass, "/config/blueprints/test.yaml")


@pytest.mark.asyncio
async def test_retry_async_client_errors_fail_fast():
    """Verify retry_async decorator does not retry on permanent HTTP 4xx errors except 429."""
    call_count = 0

    @retry_async(max_retries=3, exceptions=(httpx.HTTPError,), base_delay=0.001)
    async def fetch_something(status_code: int):
        """Fetch helper that raises HTTPStatusError with status_code."""
        nonlocal call_count
        call_count += 1
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(status_code=status_code, request=request)
        raise httpx.HTTPStatusError("HTTP Status Error", request=request, response=response)

    # Test permanent 404 Client Error - should fail fast (1 attempt, 0 retries)
    call_count = 0
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_something(httpx.codes.NOT_FOUND)
    assert call_count == 1

    # Test transient 500 Server Error - should retry (1 attempt + 3 retries = 4 calls)
    call_count = 0
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_something(httpx.codes.INTERNAL_SERVER_ERROR)
    assert call_count == 4

    # Test 429 Rate Limit - should retry (1 attempt + 3 retries = 4 calls)
    call_count = 0
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_something(httpx.codes.TOO_MANY_REQUESTS)
    assert call_count == 4

    # Test 408 Request Timeout - should retry (1 attempt + 3 retries = 4 calls)
    call_count = 0
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_something(httpx.codes.REQUEST_TIMEOUT)
    assert call_count == 4

    # Test 425 Too Early - should retry (1 attempt + 3 retries = 4 calls)
    call_count = 0
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_something(httpx.codes.TOO_EARLY)
    assert call_count == 4
