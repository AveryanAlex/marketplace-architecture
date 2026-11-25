# All analytics run over the deduplicated ClickHouse view at one ingestion cutoff.
DAILY = """
SELECT count() AS processed_events, uniqExact(user_id) AS dau,
    countIf(event_type = 'VIEW_STARTED') AS view_starts,
    countIf(event_type = 'VIEW_FINISHED') AS view_finishes,
    avgOrNullIf(progress_seconds, event_type = 'VIEW_FINISHED') AS avg_watch_seconds
FROM cinema.events
WHERE toDate(timestamp) = {date:Date} AND ingested_at <= {cutoff:DateTime64(3, 'UTC')}
"""
TOP_MOVIES = """
SELECT movie_id, countIf(event_type='VIEW_STARTED') AS views
FROM cinema.events
WHERE toDate(timestamp)={date:Date} AND ingested_at <= {cutoff:DateTime64(3, 'UTC')}
GROUP BY movie_id HAVING views > 0 ORDER BY views DESC, movie_id
"""
DEVICES = """
SELECT toString(device_type) AS device, uniqExact(user_id) AS users
FROM cinema.events
WHERE toDate(timestamp)={date:Date} AND ingested_at <= {cutoff:DateTime64(3, 'UTC')}
GROUP BY device_type
"""


def retention_sql(*, first_view: bool):
    # Cohort heatmap: first activity of any type, as requested by panel requirements.
    # D1/D7 business metrics: first VIEW_STARTED, return on a viewing event.
    first = (
        "minIf(toDate(timestamp), event_type='VIEW_STARTED')"
        if first_view
        else "min(toDate(timestamp))"
    )
    having = "HAVING countIf(event_type='VIEW_STARTED') > 0" if first_view else ""
    activity = "AND startsWith(toString(event_type), 'VIEW_')" if first_view else ""
    return f"""
    WITH first_activity AS (
        SELECT user_id, {first} AS cohort_date
        FROM cinema.events
        WHERE ingested_at <= {{cutoff:DateTime64(3, 'UTC')}}
        GROUP BY user_id {having}
    ), cohort AS (
        SELECT user_id FROM first_activity WHERE cohort_date={{date:Date}}
    ), activity AS (
        SELECT DISTINCT user_id, toDate(timestamp) AS active_date FROM cinema.events
        WHERE ingested_at <= {{cutoff:DateTime64(3, 'UTC')}}
          AND toDate(timestamp) BETWEEN {{date:Date}} AND addDays({{date:Date}}, 7)
          {activity}
    )
    SELECT dateDiff('day', {{date:Date}}, a.active_date) AS day,
        uniqExact(c.user_id) AS returned,
        (SELECT count() FROM cohort) AS cohort_size
    FROM cohort c INNER JOIN activity a ON c.user_id=a.user_id
    GROUP BY day ORDER BY day
    """
