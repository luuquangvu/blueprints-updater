"""Utility functions for Blueprints Updater."""

import asyncio
import inspect
import logging
import random
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, NoReturn, TypeVar, assert_never, cast

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")
AsyncFunc = Callable[..., Coroutine[Any, Any, _T]]


def retry_async(
    max_retries: int,
    exceptions: tuple[type[BaseException], ...],
    base_delay: float = 5.0,
    exponential: bool = True,
    jitter: bool = True,
) -> Callable[[AsyncFunc[_T]], AsyncFunc[_T]]:
    """Decorator to retry an async function with exponential backoff and jitter.

    Args:
        `max_retries`: Number of retry attempts.
        `base_delay`: Initial delay between retries.
        `exponential`: If True, use exponential backoff.
        `jitter`: If True, add random jitter to delay.
        `exceptions`: Tuple of exception types to catch and retry. This parameter
            is required to ensure that only expected errors are retried.

    Returns:
        Decorated function.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be greater than or equal to 0")
    if base_delay < 0:
        raise ValueError("base_delay must be greater than or equal to 0")

    def decorator(func: AsyncFunc[_T]) -> AsyncFunc[_T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            try:
                sig = inspect.signature(func)
                bound_args = sig.bind(*args, **kwargs)
                context = bound_args.arguments.get("url", "unknown")
            except (ValueError, TypeError):
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

            assert_never(cast(NoReturn, attempt))

        return wrapper

    return decorator
