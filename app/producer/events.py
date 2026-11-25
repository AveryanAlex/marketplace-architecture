from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class EventType(StrEnum):
    VIEW_STARTED = "VIEW_STARTED"
    VIEW_FINISHED = "VIEW_FINISHED"
    VIEW_PAUSED = "VIEW_PAUSED"
    VIEW_RESUMED = "VIEW_RESUMED"
    LIKED = "LIKED"
    SEARCHED = "SEARCHED"


class DeviceType(StrEnum):
    MOBILE = "MOBILE"
    DESKTOP = "DESKTOP"
    TV = "TV"
    TABLET = "TABLET"


class MovieEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    user_id: str = Field(min_length=1, max_length=128)
    movie_id: str = Field(min_length=1, max_length=128)
    event_type: EventType
    timestamp: AwareDatetime
    device_type: DeviceType
    session_id: str = Field(min_length=1, max_length=128)
    progress_seconds: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    app_version: str | None = Field(default=None, max_length=32)

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_representation(cls, value):
        if not isinstance(value, str | datetime):
            raise ValueError("timestamp must be an ISO 8601 datetime with timezone")
        return value

    @field_validator("timestamp")
    @classmethod
    def utc_time(cls, value):
        value = value.astimezone(UTC)
        if value < datetime(2000, 1, 1, tzinfo=UTC):
            raise ValueError("timestamp must be on or after 2000-01-01")
        if value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("timestamp may not be more than five minutes in the future")
        return value.replace(microsecond=value.microsecond // 1000 * 1000)

    @model_validator(mode="after")
    def watching_progress(self):
        if self.event_type.value.startswith("VIEW_") and self.progress_seconds is None:
            raise ValueError("progress_seconds is required for viewing events")
        if self.event_type == EventType.VIEW_STARTED and self.progress_seconds != 0:
            raise ValueError("VIEW_STARTED must have progress_seconds=0")
        return self

    def avro_record(self):
        record = self.model_dump(mode="json")
        record["timestamp"] = self.timestamp
        return record
