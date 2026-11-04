-- Keep the UTC calendar-day uniqueness without a redundant stored date attribute.
DROP INDEX flights_search_idx;
ALTER TABLE flights DROP COLUMN departure_date;
CREATE UNIQUE INDEX flights_number_departure_day_uq
    ON flights (flight_number, ((departure_at AT TIME ZONE 'UTC')::date));
CREATE INDEX flights_search_idx ON flights (origin, destination, departure_at)
    WHERE status = 'SCHEDULED';
