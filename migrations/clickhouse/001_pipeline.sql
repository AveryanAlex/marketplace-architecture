CREATE DATABASE IF NOT EXISTS cinema;

CREATE TABLE IF NOT EXISTS cinema.raw_events
(
    event_id UUID,
    user_id String,
    movie_id String,
    event_type Enum8('VIEW_STARTED'=1, 'VIEW_FINISHED'=2, 'VIEW_PAUSED'=3,
                    'VIEW_RESUMED'=4, 'LIKED'=5, 'SEARCHED'=6),
    timestamp DateTime64(3, 'UTC'),
    device_type Enum8('MOBILE'=1, 'DESKTOP'=2, 'TV'=3, 'TABLET'=4),
    session_id String,
    progress_seconds Nullable(Int32),
    app_version Nullable(String),
    kafka_partition UInt64,
    kafka_offset UInt64,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
    event_date Date MATERIALIZED toDate(timestamp)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (event_date, user_id, event_id);

-- Kafka Engine delivers at least once. Resolve retries by immutable event_id,
-- without waiting for MergeTree background merges, across all partitions/months.
CREATE VIEW IF NOT EXISTS cinema.events AS
SELECT event_id,
    record.1 AS user_id, record.2 AS movie_id, record.3 AS event_type,
    record.4 AS timestamp, record.5 AS device_type, record.6 AS session_id,
    record.7 AS progress_seconds, record.8 AS app_version,
    first_ingested_at AS ingested_at
FROM
(
    SELECT event_id,
        argMin(tuple(user_id, movie_id, event_type, timestamp, device_type,
                     session_id, progress_seconds, app_version),
               tuple(ingested_at, kafka_partition, kafka_offset)) AS record,
        min(ingested_at) AS first_ingested_at
    FROM cinema.raw_events GROUP BY event_id
);

CREATE TABLE IF NOT EXISTS cinema.ingestion_errors
(
    observed_at DateTime64(3, 'UTC'), kafka_partition UInt64,
    kafka_offset UInt64, raw_message String, error String
)
ENGINE = MergeTree ORDER BY (observed_at, kafka_partition, kafka_offset);

CREATE TABLE IF NOT EXISTS cinema.kafka_events
(
    event_id UUID, user_id String, movie_id String,
    event_type Enum8('VIEW_STARTED'=1, 'VIEW_FINISHED'=2, 'VIEW_PAUSED'=3,
                    'VIEW_RESUMED'=4, 'LIKED'=5, 'SEARCHED'=6),
    timestamp DateTime64(3, 'UTC'),
    device_type Enum8('MOBILE'=1, 'DESKTOP'=2, 'TV'=3, 'TABLET'=4),
    session_id String, progress_seconds Nullable(Int32), app_version Nullable(String)
)
ENGINE = Kafka
SETTINGS kafka_broker_list = 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
    kafka_topic_list = 'movie-events', kafka_group_name = 'clickhouse-movie-events-v1',
    kafka_format = 'AvroConfluent', kafka_num_consumers = 3,
    kafka_thread_per_consumer = 1, kafka_flush_interval_ms = 1000,
    kafka_handle_error_mode = 'stream',
    format_avro_schema_registry_url = 'http://schema-registry:8081',
    input_format_avro_allow_missing_fields = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS cinema.ingest_events TO cinema.raw_events AS
SELECT event_id, user_id, movie_id, event_type, timestamp, device_type,
    session_id, progress_seconds, app_version,
    _partition AS kafka_partition, _offset AS kafka_offset, now64(3) AS ingested_at
FROM cinema.kafka_events WHERE _error = '';

CREATE MATERIALIZED VIEW IF NOT EXISTS cinema.ingest_errors TO cinema.ingestion_errors AS
SELECT now64(3) AS observed_at, _partition AS kafka_partition,
    _offset AS kafka_offset, _raw_message AS raw_message, _error AS error
FROM cinema.kafka_events WHERE _error != '';

-- Each run writes one complete snapshot, then a commit marker. Views expose
-- only its rows; removed movie/device dimensions cannot leak from older runs.
CREATE TABLE IF NOT EXISTS cinema.metric_snapshots
(
    metric_date Date, metric_name LowCardinality(String), dimension String,
    value Nullable(Float64), computed_at DateTime64(6, 'UTC'), run_id UUID
)
ENGINE = MergeTree PARTITION BY toYYYYMM(metric_date)
ORDER BY (metric_date, run_id, metric_name, dimension);

CREATE TABLE IF NOT EXISTS cinema.aggregation_commits
(
    metric_date Date, run_id UUID, computed_at DateTime64(6, 'UTC'),
    processed_events UInt64
)
ENGINE = MergeTree ORDER BY (metric_date, computed_at, run_id);

CREATE VIEW IF NOT EXISTS cinema.daily_metrics AS
SELECT m.* FROM cinema.metric_snapshots AS m
INNER JOIN
(
    SELECT metric_date, argMax(run_id, tuple(computed_at, run_id)) AS run_id
    FROM cinema.aggregation_commits GROUP BY metric_date
) AS c ON m.metric_date = c.metric_date AND m.run_id = c.run_id;
