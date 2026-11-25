"""Live HTTP -> Kafka -> ClickHouse -> PostgreSQL -> S3 demonstration (stdlib only)."""

import base64
import json
import os
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from uuid import uuid4

MOVIE_URL = os.getenv("MOVIE_URL", "http://localhost:18180")
ANALYTICS_URL = os.getenv("ANALYTICS_URL", "http://localhost:18181")
CH_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:18123")


def request(url, data=None, *, headers=None):
    encoded = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=encoded, headers=headers or {})
    if encoded:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def query_ch(sql):
    auth = base64.b64encode(b"cinema:cinema-local").decode()
    req = urllib.request.Request(
        CH_URL, data=(sql + " FORMAT JSON").encode(), headers={"Authorization": "Basic " + auth}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)["data"]


def main():
    date = (datetime.now(UTC) - timedelta(days=1)).date()
    user = f"demo-http:{uuid4()}"
    session = str(uuid4())
    event_ids = []
    for kind, progress in [("VIEW_STARTED", 0), ("VIEW_FINISHED", 3600)]:
        payload = {
            "event_id": str(uuid4()),
            "user_id": user,
            "movie_id": "movie-demo",
            "event_type": kind,
            "timestamp": f"{date}T12:00:00Z",
            "device_type": "TV",
            "session_id": session,
            "progress_seconds": progress,
        }
        result = request(MOVIE_URL + "/events", payload)
        assert result["event_id"] == payload["event_id"]
        event_ids.append(payload["event_id"])
        print("Kafka acknowledged:", result)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        rows = query_ch(f"SELECT * FROM cinema.events WHERE user_id='{user}' ORDER BY timestamp")
        if len(rows) == 2:
            break
        time.sleep(1)
    else:
        raise AssertionError("Events did not reach ClickHouse")
    assert {r["event_id"] for r in rows} == set(event_ids)
    print("ClickHouse raw:", json.dumps(rows, indent=2))
    print("Aggregation:", request(f"{ANALYTICS_URL}/aggregations/{date}", {}))
    print(
        "PostgreSQL metrics:",
        json.dumps(request(f"{ANALYTICS_URL}/metrics?metric_date={date}"), indent=2),
    )
    print("S3 export:", request(f"{ANALYTICS_URL}/exports/{date}", {}))
    print("Dashboard: http://localhost:13000/d/cinema-analytics")


if __name__ == "__main__":
    main()
