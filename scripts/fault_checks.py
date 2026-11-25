"""Destructive only to this Compose project; every stopped service is restored."""

import json
import subprocess
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from demo import ANALYTICS_URL, MOVIE_URL, query_ch, request


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True)


def wait_healthy(service, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = subprocess.check_output(
            ["docker", "compose", "ps", "--format", "json", service], text=True
        )
        rows = [json.loads(line) for line in output.splitlines() if line]
        if rows and rows[0].get("Health") == "healthy":
            return
        time.sleep(3)
    raise AssertionError(f"{service} did not become healthy")


def publish(user):
    event_id = str(uuid4())
    response = request(
        MOVIE_URL + "/events",
        {
            "event_id": event_id,
            "user_id": user,
            "movie_id": "fault-check",
            "event_type": "VIEW_STARTED",
            "timestamp": datetime.now(UTC).isoformat(),
            "device_type": "DESKTOP",
            "session_id": str(uuid4()),
            "progress_seconds": 0,
        },
    )
    assert response["event_id"] == event_id
    return event_id


def wait_ingested(event_id):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        rows = query_ch(f"SELECT count() AS n FROM cinema.events WHERE event_id='{event_id}'")
        if int(rows[0]["n"]) == 1:
            return
        time.sleep(1)
    raise AssertionError("Event was not ingested after recovery")


def main():
    user = f"fault:{uuid4()}"
    try:
        compose("stop", "kafka-1")
        event_id = publish(user)
        wait_ingested(event_id)
        print("PASS: publication and ingestion continue with one broker/controller down")
    finally:
        compose("start", "kafka-1")
        wait_healthy("kafka-1")
    try:
        compose("stop", "clickhouse")
        event_id = publish(user)
        print("Kafka acknowledged while ClickHouse is stopped:", event_id)
    finally:
        compose("start", "clickhouse")
        wait_healthy("clickhouse")
    wait_ingested(event_id)
    print("PASS: Kafka retains events and ClickHouse catches up after restart")
    date = (datetime.now(UTC) - timedelta(days=1)).date()
    request(f"{ANALYTICS_URL}/aggregations/{date}", {})
    try:
        compose("stop", "s3")
        try:
            request(f"{ANALYTICS_URL}/exports/{date}", {})
            raise AssertionError("Unavailable S3 should return 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        output = subprocess.check_output(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "cinema",
                "-d",
                "cinema",
                "-Atc",
                f"SELECT exported_at IS NULL FROM export_jobs WHERE metric_date='{date}'",
            ],
            text=True,
        )
        assert output.strip() == "t"
    finally:
        compose("start", "s3")
        wait_healthy("s3")
    # Do not manually trigger export: prove the scheduler retries the persisted job.
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        output = subprocess.check_output(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "cinema",
                "-d",
                "cinema",
                "-Atc",
                "SELECT exported_at IS NOT NULL AND last_error IS NULL FROM export_jobs "
                f"WHERE metric_date='{date}'",
            ],
            text=True,
        )
        if output.strip() == "t":
            break
        time.sleep(5)
    else:
        raise AssertionError("Scheduler did not retry the failed export")
    print("PASS: S3 outage queues export durably; scheduler completes it after recovery")
    try:
        compose("stop", "postgres")
        try:
            request(f"{ANALYTICS_URL}/aggregations/{date}", {})
            raise AssertionError("Unavailable PostgreSQL should return 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        compose("start", "postgres")
        wait_healthy("postgres")
    result = request(f"{ANALYTICS_URL}/aggregations/{date}", {})
    assert result["date"] == str(date)
    print("PASS: aggregation resumes after PostgreSQL restart")


if __name__ == "__main__":
    main()
