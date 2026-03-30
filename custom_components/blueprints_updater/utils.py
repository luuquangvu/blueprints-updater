"""Utility functions for Blueprints Updater."""

import asyncio
import inspect
import logging
import random
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, TypeVar

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")
AsyncFunc = Callable[..., Coroutine[Any, Any, _T]]


def retry_async(
    max_retries: int,
    base_delay: float = 2.0,
    exponential: bool = True,
    jitter: bool = True,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[AsyncFunc[_T]], AsyncFunc[_T]]:
    """Decorator to retry an async function with exponential backoff and jitter."""

    def decorator(func: AsyncFunc[_T]) -> AsyncFunc[_T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            last_err: Exception = Exception("Unknown error")

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as err:
                    last_err = err

                    try:
                        sig = inspect.signature(func)
                        bound_args = sig.bind(*args, **kwargs)
                        context = bound_args.arguments.get("url", "unknown")
                    except (ValueError, TypeError):
                        context = getattr(func, "__name__", "unknown")

                    if attempt < max_retries:
                        wait = (base_delay * (2**attempt) if exponential else base_delay) + (
                            random.uniform(0, base_delay) if jitter else 0
                        )
                        _LOGGER.info(
                            "Retrying lookup for %s due to %s (Retry %d/%d, wait %.2fs)",
                            context,
                            err,
                            attempt + 1,
                            max_retries,
                            wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        _LOGGER.error(
                            "Could not update from %s after %d attempts: %s",
                            context,
                            max_retries + 1,
                            err,
                        )
                        raise last_err from err

            raise last_err from None

        return wrapper

    return decorator
