import io
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastavro import parse_schema, schemaless_reader
from pydantic import ValidationError

from app.common.config import Settings
from app.producer.events import MovieEvent
from app.producer.generator import demo_events, session_events
from app.producer.kafka import EventPublisher


def event_data():
    return {
        "event_id": str(uuid4()),
        "user_id": "alice",
        "movie_id": "matrix",
        "event_type": "VIEW_STARTED",
        "timestamp": "2026-01-01T10:00:00Z",
        "device_type": "TV",
        "session_id": "session-1",
        "progress_seconds": 0,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "bad"),
        ("timestamp", 1767268800),
        ("user_id", ""),
        ("movie_id", ""),
        ("session_id", ""),
        ("event_type", "PLAYED"),
        ("device_type", "PHONE"),
        ("progress_seconds", -1),
        ("progress_seconds", 1.5),
        ("progress_seconds", True),
        ("progress_seconds", None),
        ("progress_seconds", 1),
        ("timestamp", "2026-01-01T10:00:00"),
        ("timestamp", "1999-01-01T00:00:00Z"),
        ("timestamp", (datetime.now(UTC) + timedelta(days=1)).isoformat()),
        ("app_version", "x" * 33),
    ],
)
def test_validation(field, value):
    data = event_data() | {field: value}
    with pytest.raises(ValidationError):
        MovieEvent.model_validate(data)


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        MovieEvent.model_validate(event_data() | {"arbitrary": 1})


def test_normalizes_utc_and_milliseconds():
    event = MovieEvent.model_validate(
        event_data() | {"timestamp": "2026-01-01T13:00:00.123456+03:00"}
    )
    assert event.timestamp.isoformat() == "2026-01-01T10:00:00.123000+00:00"


def test_confluent_avro_wire_format_and_v1_compatibility():
    publisher = EventPublisher(Settings())
    publisher.schema_id = 42
    schema_v2 = json.loads(Path("schemas/movie-event-v2.avsc").read_text())
    publisher.schema = parse_schema(schema_v2)
    event = MovieEvent.model_validate(event_data())
    encoded = publisher.encode(event)
    assert struct.unpack(">bI", encoded[:5]) == (0, 42)
    decoded = schemaless_reader(io.BytesIO(encoded[5:]), publisher.schema)
    assert str(decoded["event_id"]) == str(event.event_id)
    assert decoded["timestamp"] == event.timestamp
    assert decoded["app_version"] is None
    # New readers can consume v1 records because the added field has a default.
    old_schema = parse_schema(json.loads(Path("schemas/movie-event-v1.avsc").read_text()))
    from fastavro import schemaless_writer

    output = io.BytesIO()
    old_record = event.avro_record()
    old_record.pop("app_version")
    schemaless_writer(output, old_schema, old_record)
    decoded = schemaless_reader(io.BytesIO(output.getvalue()), old_schema, publisher.schema)
    assert decoded["app_version"] is None


def test_generated_sessions_realistic():
    for seed in range(30):
        events = session_events("user", datetime(2026, 1, 1, tzinfo=UTC), seed=seed)
        kinds = [event.event_type.value for event in events]
        assert (
            kinds.index("VIEW_STARTED") < kinds.index("VIEW_PAUSED") < kinds.index("VIEW_RESUMED")
        )
        if "VIEW_FINISHED" in kinds:
            assert kinds.index("VIEW_RESUMED") < kinds.index("VIEW_FINISHED")
        progress = [
            event.progress_seconds for event in events if event.progress_seconds is not None
        ]
        assert progress == sorted(progress)
        assert [event.timestamp for event in events] == sorted(event.timestamp for event in events)
        assert len({event.session_id for event in events}) == 1


def test_demo_restart_is_idempotent():
    today = datetime(2026, 1, 20).date()
    first = list(demo_events(today))
    second = list(demo_events(today))
    assert [event.model_dump() for event in first] == [event.model_dump() for event in second]
    assert len({event.event_id for event in first}) == len(first)
    assert all(event.timestamp.date() < today for event in first)
