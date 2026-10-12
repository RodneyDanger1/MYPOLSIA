"""Retry helpers (tenacity)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

TRANSIENT = (ConnectionError, TimeoutError, OSError)


async def with_retry(
    fn: Callable[..., Any],
    *args: Any,
    attempts: int = 3,
    **kwargs: Any,
) -> Any:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=30),
        retry=retry_if_exception_type(TRANSIENT),
        reraise=True,
    ):
        with attempt:
            return await fn(*args, **kwargs)


def async_retry(attempts: int = 3):
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            return await with_retry(fn, *args, attempts=attempts, **kwargs)

        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapper

    return decorator
