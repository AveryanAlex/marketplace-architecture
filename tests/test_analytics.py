from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.analytics.service import retention_values
from app.common.retry import retry


def test_mature_and_immature_retention():
    size, values = retention_values(
        [
            {"day": 0, "returned": 10, "cohort_size": 10},
            {"day": 1, "returned": 4, "cohort_size": 10},
        ],
        date(2026, 1, 1),
        date(2026, 1, 5),
        range(8),
    )
    assert size == 10
    assert values == {0: 1, 1: 0.4, 2: 0, 3: 0, 4: None, 5: None, 6: None, 7: None}


def test_empty_retention_not_nan():
    assert retention_values([], date(2026, 1, 1), date(2026, 1, 10), [1, 7]) == (
        0,
        {1: None, 7: None},
    )


async def test_retry_eventual_success(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.common.retry.asyncio.sleep", sleep)
    operation = AsyncMock(side_effect=[ConnectionError(), ConnectionError(), 5])
    assert await retry(operation, attempts=3, name="test") == 5
    delays = [args.args[0] for args in sleep.call_args_list]
    assert len(delays) == 2
    assert delays[1] > delays[0]


async def test_retry_exhaustion(monkeypatch):
    monkeypatch.setattr("app.common.retry.asyncio.sleep", AsyncMock())
    operation = AsyncMock(side_effect=ConnectionError())
    with pytest.raises(ConnectionError):
        await retry(operation, attempts=3, name="test")
    assert operation.await_count == 3
