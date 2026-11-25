"""Idempotent, versioned startup migrations and schema registration."""

import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg
import boto3
import httpx
import structlog
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic

from app.common.clickhouse import ClickHouse
from app.common.config import settings
from app.common.logging import configure_logging
from app.common.retry import retry

configure_logging()
log = structlog.get_logger()


async def kafka_and_schemas(config):
    admin = AdminClient({"bootstrap.servers": config.kafka_bootstrap})
    metadata = await asyncio.to_thread(admin.list_topics, timeout=20)
    if config.kafka_topic not in metadata.topics:
        futures = admin.create_topics(
            [
                NewTopic(
                    config.kafka_topic,
                    num_partitions=3,
                    replication_factor=3,
                    config={"min.insync.replicas": "2", "retention.ms": "604800000"},
                )
            ]
        )
        for future in futures.values():
            await asyncio.to_thread(future.result, 30)
    metadata = await asyncio.to_thread(admin.list_topics, timeout=20)
    topic = metadata.topics[config.kafka_topic]
    if len(topic.partitions) < 3 or any(len(p.replicas) < 2 for p in topic.partitions.values()):
        raise RuntimeError("Existing movie-events topic has insufficient partitions/replication")
    resource = ConfigResource(ConfigResource.Type.TOPIC, config.kafka_topic)
    configs = await asyncio.to_thread(admin.describe_configs([resource])[resource].result, 20)
    if int(configs["min.insync.replicas"].value) < 1:
        raise RuntimeError("min.insync.replicas must be >= 1")
    async with httpx.AsyncClient(base_url=config.schema_registry_url, timeout=20) as client:
        response = await client.put(
            f"/config/{config.schema_subject}", json={"compatibility": "FULL_TRANSITIVE"}
        )
        response.raise_for_status()
        for version in (1, 2):
            schema = await asyncio.to_thread(Path(f"schemas/movie-event-v{version}.avsc").read_text)
            response = await client.post(
                f"/subjects/{config.schema_subject}/versions",
                json={"schemaType": "AVRO", "schema": schema},
            )
            response.raise_for_status()
            check = await client.get(f"/subjects/{config.schema_subject}/versions/{version}")
            check.raise_for_status()
            if json.loads(check.json()["schema"]) != json.loads(schema):
                raise RuntimeError(f"Registry version {version} differs from source")
            log.info("schema_registered", version=version, schema_id=response.json()["id"])


async def postgres_migrations(config):
    connection = await asyncpg.connect(config.postgres_dsn)
    try:
        await connection.execute("SELECT pg_advisory_lock(723145)")
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        for file in await asyncio.to_thread(
            lambda: sorted(Path("migrations/postgres").glob("*.sql"))
        ):
            source = await asyncio.to_thread(file.read_text)
            checksum = hashlib.sha256(source.encode()).hexdigest()
            previous = await connection.fetchval(
                "SELECT checksum FROM schema_migrations WHERE version=$1", file.name
            )
            if previous and previous != checksum:
                raise RuntimeError(f"Applied migration changed: {file.name}")
            if previous:
                continue
            async with connection.transaction():
                await connection.execute(source)
                await connection.execute(
                    "INSERT INTO schema_migrations(version,checksum) VALUES($1,$2)",
                    file.name,
                    checksum,
                )
            log.info("migration_applied", database="postgres", version=file.name)
    finally:
        await connection.close()


async def clickhouse_migrations(config):
    client = ClickHouse(config)
    try:
        await client.execute("""CREATE TABLE IF NOT EXISTS default.schema_migrations
            (version String, checksum String, applied_at DateTime64(3,'UTC') DEFAULT now64(3))
            ENGINE=MergeTree ORDER BY version""")
        for file in await asyncio.to_thread(
            lambda: sorted(Path("migrations/clickhouse").glob("*.sql"))
        ):
            source = await asyncio.to_thread(file.read_text)
            checksum = hashlib.sha256(source.encode()).hexdigest()
            rows = await client.query(
                "SELECT checksum FROM default.schema_migrations WHERE version={version:String}",
                {"param_version": file.name},
            )
            if rows:
                if rows[0]["checksum"] != checksum:
                    raise RuntimeError(f"Applied migration changed: {file.name}")
                continue
            # DDL is idempotent; failed startup can resume after a partial ClickHouse migration.
            sql = "\n".join(
                line for line in source.splitlines() if not line.lstrip().startswith("--")
            )
            for statement in sql.split(";"):
                if statement.strip():
                    await client.execute(statement)
            await client.insert(
                "default.schema_migrations", [{"version": file.name, "checksum": checksum}]
            )
            log.info("migration_applied", database="clickhouse", version=file.name)
        await client.execute("""CREATE USER IF NOT EXISTS grafana
            IDENTIFIED WITH sha256_password BY 'grafana-local'""")
        await client.execute("GRANT SELECT ON cinema.daily_metrics TO grafana")
        await client.execute("GRANT SELECT ON cinema.metric_snapshots TO grafana")
        await client.execute("GRANT SELECT ON cinema.aggregation_commits TO grafana")
        await client.execute("""ALTER USER grafana SETTINGS readonly=1,
            limit=0 CHANGEABLE_IN_READONLY, max_execution_time=60 CHANGEABLE_IN_READONLY""")
    finally:
        await client.close()


def create_bucket(config):
    s3 = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
        region_name="us-east-1",
    )
    buckets = s3.list_buckets()["Buckets"]
    if config.s3_bucket not in {bucket["Name"] for bucket in buckets}:
        s3.create_bucket(Bucket=config.s3_bucket)


async def main():
    config = settings()
    for name, operation in [
        ("kafka_schema_setup", lambda: kafka_and_schemas(config)),
        ("postgres_migrations", lambda: postgres_migrations(config)),
        ("clickhouse_migrations", lambda: clickhouse_migrations(config)),
        ("s3_bucket_setup", lambda: asyncio.to_thread(create_bucket, config)),
    ]:
        await retry(operation, attempts=8, name=name)
    log.info("bootstrap_completed")


if __name__ == "__main__":
    asyncio.run(main())
