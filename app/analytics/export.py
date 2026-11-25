import asyncio
import json

import boto3
import structlog
from botocore.config import Config

from app.common.retry import retry

log = structlog.get_logger()
LOCK_KEY = 8137021


class Exporter:
    def __init__(self, config, pool):
        self.config = config
        self.pool = pool
        self.s3 = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name="us-east-1",
            config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1}),
        )

    async def export(self, date):
        # Serializes snapshot changes and exports across replicas; prevents an older
        # export from overwriting a newer one after a slow object-storage request.
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
            run = await conn.fetchrow("SELECT * FROM aggregation_runs WHERE metric_date=$1", date)
            if not run:
                raise ValueError("No computed aggregates for this date")
            rows = await conn.fetch(
                "SELECT * FROM daily_metrics WHERE metric_date=$1 ORDER BY metric_name,dimension",
                date,
            )
            body = json.dumps(
                {
                    "format_version": 1,
                    "date": date.isoformat(),
                    "computed_at": run["computed_at"].isoformat(),
                    "run_id": str(run["run_id"]),
                    "processed_events": run["processed_events"],
                    "metrics": [
                        {
                            "date": row["metric_date"].isoformat(),
                            "name": row["metric_name"],
                            "dimension": row["dimension"],
                            "value": row["value"],
                            "computed_at": row["computed_at"].isoformat(),
                        }
                        for row in rows
                    ],
                },
                allow_nan=False,
                sort_keys=True,
            ).encode()
            key = f"daily/{date.isoformat()}/aggregates.json"

            async def put():
                await asyncio.to_thread(
                    self.s3.put_object,
                    Bucket=self.config.s3_bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                )

            await retry(put, attempts=self.config.retry_attempts, name="s3_put")
            await conn.execute(
                """
                INSERT INTO export_jobs(metric_date, exported_at, exported_run_id, attempts)
                VALUES ($1, now(), $2, 1)
                ON CONFLICT(metric_date) DO UPDATE SET exported_at=now(),
                    exported_run_id=$2, attempts=export_jobs.attempts+1, last_error=NULL
                """,
                date,
                run["run_id"],
            )
            log.info("export_completed", date=str(date), key=key, metrics=len(rows))
            return {"date": str(date), "key": key, "metrics": len(rows), "bytes": len(body)}

    async def request(self, date):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO export_jobs(metric_date) VALUES($1)
                ON CONFLICT(metric_date) DO UPDATE SET requested_at=now(), exported_at=NULL
                """,
                date,
            )

    async def run_pending(self):
        # Older failed exports stay in the durable queue, regardless of lookback.
        async with self.pool.acquire() as conn:
            dates = await conn.fetch("""
                SELECT j.metric_date FROM export_jobs j
                JOIN aggregation_runs r USING(metric_date)
                WHERE j.metric_date < (now() AT TIME ZONE 'UTC')::date
                  AND (j.exported_at IS NULL OR j.exported_run_id IS DISTINCT FROM r.run_id)
                ORDER BY j.metric_date
                """)
        for record in dates:
            date = record["metric_date"]
            try:
                await self.export(date)
            except Exception as exc:
                log.error("export_failed", date=str(date), error=type(exc).__name__)
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE export_jobs SET attempts=attempts+1, last_error=$2
                        WHERE metric_date=$1
                        """,
                        date,
                        type(exc).__name__,
                    )

    async def health(self):
        await asyncio.to_thread(self.s3.head_bucket, Bucket=self.config.s3_bucket)

    def close(self):
        self.s3.close()
