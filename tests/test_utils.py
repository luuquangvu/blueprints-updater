"""Tests for Blueprints Updater utilities."""

from unittest.mock import MagicMock

import pytest

from custom_components.blueprints_updater.utils import retry_async


@pytest.mark.asyncio
async def test_retry_async_success():
    """Test retry_async decorator when it succeeds immediately."""
    mock = MagicMock()

    async def mock_func() -> str:
        """Real async function to satisfy IDE, calling mock to track execution."""
        mock()
        return "success"

    @retry_async(max_retries=3)
    async def decorated_func():
        return await mock_func()

    result = await decorated_func()
    assert result == "success"
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_retry_async_retry_success():
    """Test retry_async decorator when it succeeds after some retries."""
    call_count = 0

    @retry_async(max_retries=3, base_delay=0.01)
    async def decorated_func():
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

    @retry_async(max_retries=2, base_delay=0.01)
    async def decorated_func():
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

    @retry_async(max_retries=3, base_delay=0.01, exceptions=(ValueError,))
    async def decorated_func():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("Retryable")
        raise TypeError("Not retryable")

    with pytest.raises(TypeError, match="Not retryable"):
        await decorated_func()
    assert call_count == 2
