CREATE TABLE daily_metrics (
    metric_date DATE NOT NULL,
    metric_name TEXT NOT NULL,
    dimension TEXT NOT NULL DEFAULT '',
    value DOUBLE PRECISION,
    computed_at TIMESTAMPTZ NOT NULL,
    run_id UUID NOT NULL,
    PRIMARY KEY (metric_date, metric_name, dimension)
);
CREATE INDEX daily_metrics_name_date ON daily_metrics (metric_name, metric_date);
CREATE TABLE aggregation_runs (
    metric_date DATE PRIMARY KEY,
    run_id UUID NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    processed_events BIGINT NOT NULL CHECK (processed_events >= 0)
);
CREATE TABLE export_jobs (
    metric_date DATE PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    exported_at TIMESTAMPTZ,
    exported_run_id UUID,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
