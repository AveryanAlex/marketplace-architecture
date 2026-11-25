import asyncio
import random
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger()


async def retry[T](operation: Callable[[], Awaitable[T]], *, attempts: int = 3, name: str) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            log.warning(
                "operation_failed", operation=name, attempt=attempt, error=type(exc).__name__
            )
            if attempt == attempts:
                raise
            await asyncio.sleep(min(0.5 * 2 ** (attempt - 1), 8) + random.uniform(0, 0.1))
    raise AssertionError("unreachable")
