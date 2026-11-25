import asyncio
import json
import os
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import boto3
import httpx
import pytest
import pytest_asyncio
from confluent_kafka.admin import AdminClient, ConfigResource

from app.common.clickhouse import ClickHouse
from app.common.config import Settings
from app.producer.events import MovieEvent
from app.producer.kafka import EventPublisher

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def stack():
    config = Settings()
    ch = ClickHouse(config)
    pool = await asyncpg.create_pool(config.postgres_dsn, min_size=1, max_size=2)
    async with (
        httpx.AsyncClient(
            base_url=os.getenv("MOVIE_URL", "http://localhost:18180"), timeout=90
        ) as movie,
        httpx.AsyncClient(
            base_url=os.getenv("ANALYTICS_URL", "http://localhost:18181"), timeout=180
        ) as analytics,
    ):
        # Reserve an unused 8-day historical window; test data never intersects demo/live dates.
        existing = await ch.query(
            "SELECT DISTINCT toDate(timestamp) AS date FROM cinema.raw_events"
        )
        occupied = {date.fromisoformat(row["date"]) for row in existing}
        for _ in range(100):
            day = date(2000, 1, 1) + timedelta(days=random.randrange(365, 6500))
            if not any(day + timedelta(days=offset) in occupied for offset in range(8)):
                break
        else:
            raise AssertionError("Cannot find an isolated test window")
        prefix = f"test:{uuid4()}:"
        yield config, ch, pool, movie, analytics, day, prefix
    # Only this test's events/metrics/exports are removed, including failed assertions.
    await ch.execute(
        "ALTER TABLE cinema.raw_events DELETE WHERE startsWith(user_id,{prefix:String}) "
        "SETTINGS mutations_sync=1",
        {"param_prefix": prefix},
    )
    await ch.execute(
        "ALTER TABLE cinema.metric_snapshots DELETE WHERE metric_date BETWEEN "
        "{start:Date} AND {end:Date} SETTINGS mutations_sync=1",
        {"param_start": str(day), "param_end": str(day + timedelta(days=7))},
    )
    await ch.execute(
        "ALTER TABLE cinema.aggregation_commits DELETE WHERE metric_date BETWEEN "
        "{start:Date} AND {end:Date} SETTINGS mutations_sync=1",
        {"param_start": str(day), "param_end": str(day + timedelta(days=7))},
    )
    async with pool.acquire() as conn:
        for table in ["daily_metrics", "aggregation_runs", "export_jobs"]:
            await conn.execute(
                f"DELETE FROM {table} WHERE metric_date BETWEEN $1 AND $2",
                day,
                day + timedelta(days=7),
            )
    s3 = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
    )
    for offset in range(8):
        await asyncio.to_thread(
            s3.delete_object,
            Bucket=config.s3_bucket,
            Key=f"daily/{day + timedelta(days=offset)}/aggregates.json",
        )
    s3.close()
    await pool.close()
    await ch.close()


def event(day, user, kind="VIEW_STARTED", progress=0, *, movie="movie-test", session=None):
    return {
        "event_id": str(uuid4()),
        "user_id": user,
        "movie_id": movie,
        "event_type": kind,
        "timestamp": datetime.combine(day, datetime.min.time(), UTC).replace(hour=12).isoformat(),
        "device_type": "TV",
        "session_id": session or str(uuid4()),
        "progress_seconds": progress,
    }


async def wait_rows(ch, prefix, expected):
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        rows = await ch.query(
            "SELECT * FROM cinema.events WHERE startsWith(user_id,{prefix:String})",
            {"param_prefix": prefix},
        )
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.5)
    raise AssertionError(f"Only {len(rows)} / {expected} events arrived in ClickHouse")


async def test_event_reaches_clickhouse_and_deduplicates(stack):
    _, ch, _, movie, _, day, prefix = stack
    payload = event(day, prefix + "alice") | {"app_version": "test-2"}
    replies = []
    for _ in range(2):
        response = await movie.post("/events", json=payload)
        assert response.status_code == 201, response.text
        replies.append(response.json())
    assert replies[0]["event_id"] == payload["event_id"]
    assert replies[0]["partition"] == replies[1]["partition"]
    assert replies[0]["offset"] < replies[1]["offset"]
    rows = await wait_rows(ch, prefix, 1)
    row = rows[0]
    assert row["event_id"] == payload["event_id"]
    assert row["user_id"] == payload["user_id"]
    assert row["movie_id"] == payload["movie_id"]
    assert row["event_type"] == "VIEW_STARTED"
    assert row["timestamp"] == f"{day} 12:00:00.000"
    assert row["device_type"] == "TV"
    assert row["session_id"] == payload["session_id"]
    assert row["progress_seconds"] == 0
    assert row["app_version"] == "test-2"
    await asyncio.sleep(1.5)
    rows = await wait_rows(ch, prefix, 1)
    assert len(rows) == 1


async def test_invalid_event_never_published(stack):
    _, ch, _, movie, _, day, prefix = stack
    payload = event(day, prefix + "invalid") | {"event_type": "UNKNOWN"}
    response = await movie.post("/events", json=payload)
    assert response.status_code == 422
    rows = await ch.query(
        "SELECT count() AS n FROM cinema.raw_events WHERE user_id={user:String}",
        {"param_user": payload["user_id"]},
    )
    assert int(rows[0]["n"]) == 0


async def test_all_metrics_postgres_clickhouse_export_idempotence(stack):
    config, ch, pool, movie, analytics, day, prefix = stack
    events = []
    for user in ("a", "b"):
        session = str(uuid4())
        events.append(event(day, prefix + user, session=session))
        events.append(
            event(day, prefix + user, "VIEW_FINISHED", 100 if user == "a" else 300, session=session)
        )
    # a returns D1 and D7; b does not. One start and finish each return day.
    for offset in (1, 7):
        session = str(uuid4())
        events.extend(
            [
                event(day + timedelta(days=offset), prefix + "a", session=session),
                event(
                    day + timedelta(days=offset),
                    prefix + "a",
                    "VIEW_FINISHED",
                    200,
                    session=session,
                ),
            ]
        )
    for payload in events:
        response = await movie.post("/events", json=payload)
        assert response.status_code == 201, response.text
    await wait_rows(ch, prefix, len(events))
    response = await analytics.post(f"/aggregations/{day}")
    assert response.status_code == 200, response.text
    assert response.json()["processed_events"] == 4
    rows = (await analytics.get("/metrics", params={"metric_date": str(day)})).json()
    metrics = {(r["metric_name"], r["dimension"]): r["value"] for r in rows}
    assert metrics[("dau", "")] == 2
    assert metrics[("avg_watch_seconds", "")] == 200
    assert metrics[("view_conversion", "")] == 1
    assert metrics[("top_movie_views", "movie-test")] == 2
    assert metrics[("device_users", "TV")] == 2
    assert metrics[("retention_d1", "")] == 0.5
    assert metrics[("retention_d7", "")] == 0.5
    assert metrics[("cohort_retention", "0")] == 1
    assert metrics[("cohort_retention", "1")] == 0.5
    assert metrics[("cohort_retention", "2")] == 0
    assert metrics[("cohort_retention", "7")] == 0.5
    ch_rows = await ch.query(
        "SELECT metric_name,dimension,value FROM cinema.daily_metrics "
        "WHERE metric_date={date:Date}",
        {"param_date": str(day)},
    )
    assert {(r["metric_name"], r["dimension"]): r["value"] for r in ch_rows} == metrics
    before_count = len(rows)
    response = await analytics.post(f"/aggregations/{day}")
    assert response.status_code == 200, response.text
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM daily_metrics WHERE metric_date=$1", day)
            == before_count
        )
    ch_rows = await ch.query(
        "SELECT count() AS n FROM cinema.daily_metrics WHERE metric_date={date:Date}",
        {"param_date": str(day)},
    )
    assert int(ch_rows[0]["n"]) == before_count
    for _ in range(2):
        response = await analytics.post(f"/exports/{day}")
        assert response.status_code == 200, response.text
    s3 = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
    )
    key = f"daily/{day}/aggregates.json"
    obj = await asyncio.to_thread(s3.get_object, Bucket=config.s3_bucket, Key=key)
    exported = json.loads(obj["Body"].read())
    assert exported["date"] == str(day)
    assert exported["computed_at"]
    assert len(exported["metrics"]) == before_count
    assert {(r["name"], r["dimension"]): r["value"] for r in exported["metrics"]} == metrics
    listing = await asyncio.to_thread(
        s3.list_objects_v2, Bucket=config.s3_bucket, Prefix=f"daily/{day}/"
    )
    assert [obj["Key"] for obj in listing["Contents"]] == [key]
    s3.close()


async def test_v1_avro_ingestion(stack):
    config, ch, _, _, _, day, prefix = stack
    publisher = EventPublisher(config.model_copy(update={"schema_version": 1}))
    await publisher.start()
    try:
        payload = MovieEvent.model_validate(event(day, prefix + "v1"))
        await publisher.publish(payload)
        rows = await wait_rows(ch, prefix, 1)
        assert rows[0]["app_version"] is None
    finally:
        await publisher.close()


async def test_kafka_topology_and_registry(stack):
    config, _, _, _, _, _, _ = stack
    admin = AdminClient({"bootstrap.servers": config.kafka_bootstrap})
    metadata = await asyncio.to_thread(admin.list_topics, config.kafka_topic, 10)
    assert len(metadata.brokers) == 3
    partitions = metadata.topics[config.kafka_topic].partitions
    assert len(partitions) >= 3
    assert all(len(p.replicas) >= 2 for p in partitions.values())
    resource = ConfigResource(ConfigResource.Type.TOPIC, config.kafka_topic)
    configs = await asyncio.to_thread(admin.describe_configs([resource])[resource].result, 10)
    assert int(configs["min.insync.replicas"].value) >= 1
    async with httpx.AsyncClient(base_url=config.schema_registry_url) as registry:
        response = await registry.get(f"/subjects/{config.schema_subject}/versions")
        assert response.json() == [1, 2]
        response = await registry.get(f"/config/{config.schema_subject}")
        assert response.json()["compatibilityLevel"] == "FULL_TRANSITIVE"
        schema = json.loads(await asyncio.to_thread(Path("schemas/movie-event-v2.avsc").read_text))
        schema["fields"].append({"name": "required_without_default", "type": "string"})
        response = await registry.post(
            f"/compatibility/subjects/{config.schema_subject}/versions/latest",
            json={"schemaType": "AVRO", "schema": json.dumps(schema)},
        )
        assert response.status_code == 200
        assert response.json()["is_compatible"] is False


async def test_grafana_datasource_and_all_dashboard_queries(stack):
    _, ch, _, _, _, _, _ = stack
    dashboard = json.loads(
        await asyncio.to_thread(Path("deploy/grafana/dashboards/cinema.json").read_text)
    )
    async with httpx.AsyncClient(
        base_url=os.getenv("GRAFANA_URL", "http://localhost:13000"),
        auth=("admin", "cinema-grafana-local"),
        timeout=30,
    ) as grafana:
        response = await grafana.get("/api/datasources/uid/cinema-clickhouse/health")
        assert response.status_code == 200, response.text
        response = await grafana.get("/api/dashboards/uid/cinema-analytics")
        assert response.status_code == 200, response.text
        assert len(response.json()["dashboard"]["panels"]) >= 3
        for panel in dashboard["panels"]:
            sql = panel["targets"][0]["rawSql"].replace(
                "$__dateFilter(metric_date)", "metric_date >= today()-14"
            )
            assert "raw_events" not in sql and "cinema.events" not in sql
            await ch.query(sql)
            # Execute through the real plugin too (macro expansion + read-only credentials).
            target = panel["targets"][0] | {
                "datasource": {"uid": "cinema-clickhouse", "type": "grafana-clickhouse-datasource"},
                "intervalMs": 60000,
                "maxDataPoints": 1000,
            }
            response = await grafana.post(
                "/api/ds/query",
                json={
                    "from": str(int((datetime.now(UTC) - timedelta(days=14)).timestamp() * 1000)),
                    "to": str(int(datetime.now(UTC).timestamp() * 1000)),
                    "queries": [target],
                },
            )
            assert response.status_code == 200, response.text
            assert not response.json()["results"]["A"].get("error"), response.text


async def test_empty_day_replaces_stale_dimensions_and_ignores_uncommitted_snapshot(stack):
    _, ch, _, movie, analytics, day, prefix = stack
    response = await movie.post("/events", json=event(day, prefix + "replace"))
    assert response.status_code == 201
    await wait_rows(ch, prefix, 1)
    response = await analytics.post(f"/aggregations/{day}")
    assert response.status_code == 200, response.text
    await ch.execute(
        "ALTER TABLE cinema.raw_events DELETE WHERE startsWith(user_id,{prefix:String}) "
        "SETTINGS mutations_sync=1",
        {"param_prefix": prefix},
    )
    # Concurrent manual runs must serialize across the distributed lock.
    responses = await asyncio.gather(*[analytics.post(f"/aggregations/{day}") for _ in range(3)])
    assert all(response.status_code == 200 for response in responses)
    metrics = (await analytics.get("/metrics", params={"metric_date": str(day)})).json()
    values = {(r["metric_name"], r["dimension"]): r["value"] for r in metrics}
    assert values[("dau", "")] == 0
    assert values[("view_conversion", "")] is None
    assert values[("avg_watch_seconds", "")] is None
    assert values[("retention_d1", "")] is None
    assert not any(name in ("top_movie_views", "device_users") for name, _ in values)
    await ch.insert(
        "cinema.metric_snapshots",
        [
            {
                "metric_date": str(day),
                "metric_name": "dau",
                "dimension": "",
                "value": 9999,
                "computed_at": "2099-01-01 00:00:00.000000",
                "run_id": str(uuid4()),
            }
        ],
    )
    rows = await ch.query(
        "SELECT metric_name,dimension,value FROM cinema.daily_metrics "
        "WHERE metric_date={date:Date}",
        {"param_date": str(day)},
    )
    assert {(r["metric_name"], r["dimension"]): r["value"] for r in rows} == values


async def test_first_activity_and_first_view_are_distinct_cohorts(stack):
    _, ch, _, movie, analytics, day, prefix = stack
    user = prefix + "cohort"
    events = [event(day, user, "SEARCHED", None)]
    events.extend(event(day + timedelta(days=i), user) for i in (1, 2))
    for payload in events:
        response = await movie.post("/events", json=payload)
        assert response.status_code == 201
    await wait_rows(ch, prefix, 3)
    for offset in (0, 1):
        response = await analytics.post(f"/aggregations/{day + timedelta(days=offset)}")
        assert response.status_code == 200, response.text
    rows = (await analytics.get("/metrics", params={"metric_date": str(day)})).json()
    values = {(r["metric_name"], r["dimension"]): r["value"] for r in rows}
    assert values[("cohort_size", "")] == 1
    assert values[("cohort_retention", "1")] == 1
    assert values[("view_cohort_size", "")] == 0
    assert values[("retention_d1", "")] is None
    rows = (
        await analytics.get("/metrics", params={"metric_date": str(day + timedelta(days=1))})
    ).json()
    values = {(r["metric_name"], r["dimension"]): r["value"] for r in rows}
    assert values[("cohort_size", "")] == 0
    assert values[("view_cohort_size", "")] == 1
    assert values[("retention_d1", "")] == 1


async def test_export_requires_computed_snapshot_and_date_validation(stack):
    _, _, _, _, analytics, day, _ = stack
    response = await analytics.post(f"/exports/{day}")
    assert response.status_code == 404
    response = await analytics.post("/aggregations/1999-01-01")
    assert response.status_code == 422
    response = await analytics.post("/aggregations/2099-01-01")
    assert response.status_code == 422


async def test_corrupt_avro_quarantined_without_blocking_valid_events(stack):
    from confluent_kafka import Producer

    config, ch, _, movie, _, day, prefix = stack
    marker = f"not-avro:{uuid4()}"
    producer = Producer({"bootstrap.servers": config.kafka_bootstrap, "acks": "all"})
    producer.produce(config.kafka_topic, key=(prefix + "poison").encode(), value=marker.encode())
    assert await asyncio.to_thread(producer.flush, 15) == 0
    response = await movie.post("/events", json=event(day, prefix + "valid"))
    assert response.status_code == 201
    await wait_rows(ch, prefix, 1)
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        rows = await ch.query(
            "SELECT error FROM cinema.ingestion_errors WHERE raw_message={marker:String}",
            {"param_marker": marker},
        )
        if rows:
            break
        await asyncio.sleep(0.5)
    assert rows and rows[0]["error"]
    await ch.execute(
        "ALTER TABLE cinema.ingestion_errors DELETE WHERE raw_message={marker:String} "
        "SETTINGS mutations_sync=1",
        {"param_marker": marker},
    )
