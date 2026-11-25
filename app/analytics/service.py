import asyncio
from datetime import UTC, date, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import structlog

from app.analytics.export import LOCK_KEY
from app.analytics.queries import DAILY, DEVICES, TOP_MOVIES, retention_sql
from app.common.retry import retry

log = structlog.get_logger()


def retention_values(rows, cohort_date, as_of, days):
    cohort_size = int(rows[0]["cohort_size"]) if rows else 0
    returned = {int(row["day"]): int(row["returned"]) for row in rows}
    return cohort_size, {
        day: (
            returned.get(day, 0) / cohort_size
            if cohort_size and cohort_date + timedelta(days=day) < as_of
            else None
        )
        for day in days
    }


class Aggregator:
    def __init__(self, config, clickhouse, pool, exporter):
        self.config = config
        self.ch = clickhouse
        self.pool = pool
        self.exporter = exporter
        self._cycle_lock = asyncio.Lock()

    async def compute(self, metric_date: date):
        started = perf_counter()
        log.info("aggregation_started", date=str(metric_date))
        try:
            result = await retry(
                lambda: self._compute(metric_date),
                attempts=self.config.retry_attempts,
                name="aggregate_date",
            )
        except Exception as exc:
            log.error("aggregation_failed", date=str(metric_date), error=type(exc).__name__)
            raise
        log.info(
            "aggregation_completed",
            date=str(metric_date),
            processed_events=result["processed_events"],
            metrics=result["metrics"],
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )
        return result

    async def _compute(self, metric_date):
        async with self._cycle_lock, self.pool.acquire() as conn:
            # One distributed lock spans CH snapshot + PG publication. Connection
            # loss releases it automatically. Each replacement is atomic in PG.
            await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
            try:
                computed_at = datetime.now(UTC)
                cutoff = computed_at.replace(microsecond=computed_at.microsecond // 1000 * 1000)
                params = {
                    "param_date": str(metric_date),
                    "param_cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                }
                daily = (await self.ch.query(DAILY, params))[0]
                movies = await self.ch.query(TOP_MOVIES, params)
                devices = await self.ch.query(DEVICES, params)
                cohorts = await self.ch.query(retention_sql(first_view=False), params)
                viewing = await self.ch.query(retention_sql(first_view=True), params)
                starts = int(daily["view_starts"])
                finishes = int(daily["view_finishes"])
                values = [
                    ("dau", "", float(daily["dau"])),
                    ("view_starts", "", float(starts)),
                    ("view_finishes", "", float(finishes)),
                    ("avg_watch_seconds", "", daily["avg_watch_seconds"]),
                    ("view_conversion", "", finishes / starts if starts else None),
                ]
                values.extend(("top_movie_views", r["movie_id"], float(r["views"])) for r in movies)
                values.extend(("device_users", r["device"], float(r["users"])) for r in devices)
                cohort_size, heatmap = retention_values(
                    cohorts, metric_date, computed_at.date(), range(8)
                )
                view_cohort_size, retention = retention_values(
                    viewing, metric_date, computed_at.date(), [1, 7]
                )
                values.append(("cohort_size", "", float(cohort_size)))
                values.append(("view_cohort_size", "", float(view_cohort_size)))
                values.extend(
                    ("cohort_retention", str(day), value) for day, value in heatmap.items()
                )
                values.extend((f"retention_d{day}", "", value) for day, value in retention.items())
                run_id = uuid4()
                ch_time = computed_at.strftime("%Y-%m-%d %H:%M:%S.%f")
                rows = [
                    {
                        "metric_date": str(metric_date),
                        "metric_name": name,
                        "dimension": dimension,
                        "value": value,
                        "computed_at": ch_time,
                        "run_id": str(run_id),
                    }
                    for name, dimension, value in values
                ]
                await self.ch.insert("cinema.metric_snapshots", rows)
                # Commit marker only after the entire snapshot is safely written.
                await self.ch.insert(
                    "cinema.aggregation_commits",
                    [
                        {
                            "metric_date": str(metric_date),
                            "run_id": str(run_id),
                            "computed_at": ch_time,
                            "processed_events": int(daily["processed_events"]),
                        }
                    ],
                )
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM daily_metrics WHERE metric_date=$1", metric_date
                    )
                    await conn.executemany(
                        """
                        INSERT INTO daily_metrics
                        (metric_date,metric_name,dimension,value,computed_at,run_id)
                        VALUES($1,$2,$3,$4,$5,$6)
                        """,
                        [
                            (metric_date, name, dimension, value, computed_at, run_id)
                            for name, dimension, value in values
                        ],
                    )
                    await conn.execute(
                        """
                        INSERT INTO aggregation_runs VALUES($1,$2,$3,$4)
                        ON CONFLICT(metric_date) DO UPDATE SET run_id=$2, computed_at=$3,
                            processed_events=$4
                        """,
                        metric_date,
                        run_id,
                        computed_at,
                        int(daily["processed_events"]),
                    )
                    await conn.execute(
                        """
                        INSERT INTO export_jobs(metric_date) VALUES($1)
                        ON CONFLICT(metric_date) DO UPDATE SET requested_at=now(), exported_at=NULL
                        """,
                        metric_date,
                    )
                return {
                    "date": str(metric_date),
                    "run_id": str(run_id),
                    "processed_events": int(daily["processed_events"]),
                    "metrics": len(values),
                }
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)

    async def cycle(self):
        started = perf_counter()
        log.info("aggregation_cycle_started")
        today = datetime.now(UTC).date()
        processed = 0
        # Recompute previous cohorts as new D1..D7 activity arrives, plus today.
        for offset in range(self.config.aggregation_lookback_days, -1, -1):
            try:
                result = await self.compute(today - timedelta(days=offset))
                processed += result["processed_events"]
            except Exception as exc:
                log.error("scheduled_date_failed", offset=offset, error=type(exc).__name__)
        try:
            await self.exporter.run_pending()
        except Exception as exc:
            log.error("export_cycle_failed", error=type(exc).__name__)
        log.info(
            "aggregation_cycle_completed",
            processed_events=processed,
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )

    async def scheduler(self):
        while True:
            try:
                await self.cycle()
            except Exception as exc:
                log.error("scheduler_cycle_failed", error=type(exc).__name__)
            await asyncio.sleep(self.config.aggregation_interval_seconds)
