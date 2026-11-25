import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import asyncpg
from fastapi import FastAPI, HTTPException

from app.analytics.export import Exporter
from app.analytics.service import Aggregator
from app.common.clickhouse import ClickHouse
from app.common.config import settings
from app.common.logging import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(app):
    config = settings()
    pool = await asyncpg.create_pool(
        config.postgres_dsn, min_size=1, max_size=5, command_timeout=180
    )
    ch = ClickHouse(config)
    exporter = Exporter(config, pool)
    aggregator = Aggregator(config, ch, pool, exporter)
    app.state.pool = pool
    app.state.ch = ch
    app.state.exporter = exporter
    app.state.aggregator = aggregator
    task = asyncio.create_task(aggregator.scheduler()) if config.scheduler_enabled else None
    yield
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    exporter.close()
    await ch.close()
    await pool.close()


app = FastAPI(title="Cinema Analytics", version="1.0.0", lifespan=lifespan)


def check_date(value: date):
    if value < date(2000, 1, 1) or value > datetime.now(UTC).date():
        raise HTTPException(422, "Date must be between 2000-01-01 and today (UTC)")


@app.get("/health")
async def health():
    try:
        async with app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await app.state.ch.query("SELECT 1")
    except Exception as exc:
        raise HTTPException(503, "Analytics storage unavailable") from exc
    # S3 does not gate computations; its failures remain in durable export_jobs.
    return {"status": "ok"}


@app.post("/aggregations/{metric_date}")
async def compute(metric_date: date):
    check_date(metric_date)
    try:
        return await app.state.aggregator.compute(metric_date)
    except Exception as exc:
        raise HTTPException(503, "Aggregation failed; safe to retry") from exc


@app.post("/exports/{metric_date}")
async def export(metric_date: date):
    check_date(metric_date)
    try:
        await app.state.exporter.request(metric_date)
        return await app.state.exporter.export(metric_date)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Export queued; scheduler will retry") from exc


@app.get("/metrics")
async def metrics(metric_date: date):
    check_date(metric_date)
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM daily_metrics WHERE metric_date=$1 ORDER BY metric_name,dimension",
            metric_date,
        )
    return [dict(row) for row in rows]
