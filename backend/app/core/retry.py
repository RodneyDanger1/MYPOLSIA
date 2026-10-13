"""Retry helpers (tenacity).

UNWIRED (audit RD15 dead-code sweep, 2026-10-13): no module in this tree imports this file —
grep evidence in `polsia-work/build/RD15-deadsweep/raw/retry_importers.txt`; it has had ZERO
callers since it was added (reconstruction checkpoint `e7108bf`). The retry ladders that DO run
are `claude_cli.run_with_retries` (agent dispatch, `base_agent.py:385`), celery's `self.retry`
ladder (`celery_app/tasks/agent_tasks.py`), and redis-py's `Retry` (`app/core/redis_client.py`).
Kept deliberately (additive lane policy): removal vs adoption is an owner decision (REQUESTS
RD15-1) — do NOT wire this in without that call; it would add a fourth, competing retry ladder
on live paths.
"""
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
