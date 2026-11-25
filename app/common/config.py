from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_bootstrap: str = "kafka-1:9092,kafka-2:9092,kafka-3:9092"
    kafka_topic: str = "movie-events"
    schema_registry_url: str = "http://schema-registry:8081"
    schema_subject: str = "movie-events-value"
    schema_version: int = 2
    publish_attempts: int = Field(default=3, ge=1, le=10)
    publish_timeout_seconds: float = Field(default=15, gt=0)
    clickhouse_url: str = "http://clickhouse:8123"
    clickhouse_user: str = "cinema"
    clickhouse_password: str = "cinema-local"
    postgres_dsn: str = "postgresql://cinema:cinema-local@postgres:5432/cinema"
    s3_endpoint: str = "http://s3:8333"
    s3_access_key: str = "cinema-s3"
    s3_secret_key: str = "cinema-s3-local"
    s3_bucket: str = "movie-analytics"
    aggregation_interval_seconds: float = Field(default=60, ge=1)
    aggregation_lookback_days: int = Field(default=9, ge=8, le=365)
    scheduler_enabled: bool = True
    generator_enabled: bool = True
    generator_interval_seconds: float = Field(default=3, ge=0.1)
    demo_seed_enabled: bool = True
    retry_attempts: int = Field(default=3, ge=1, le=10)


@lru_cache
def settings() -> Settings:
    return Settings()
