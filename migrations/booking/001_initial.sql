CREATE TYPE booking_status AS ENUM ('CONFIRMED', 'CANCELLED');
CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    user_id text NOT NULL CHECK (length(user_id) BETWEEN 1 AND 128),
    flight_id uuid NOT NULL,
    passenger_name text NOT NULL CHECK (length(passenger_name) BETWEEN 1 AND 200),
    passenger_email text NOT NULL CHECK (length(passenger_email) BETWEEN 3 AND 254),
    seat_count integer NOT NULL CHECK (seat_count BETWEEN 1 AND 1000),
    total_price_minor bigint NOT NULL CHECK (total_price_minor > 0),
    status booking_status NOT NULL DEFAULT 'CONFIRMED',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bookings_user_idx ON bookings(user_id, created_at DESC);
-- Durable saga intents, not client-visible bookings. Created before ReserveSeats.
CREATE TABLE booking_attempts (
    id uuid PRIMARY KEY,
    fingerprint text NOT NULL,
    state text NOT NULL DEFAULT 'PROCESSING'
        CHECK (state IN ('PROCESSING', 'SUCCEEDED', 'COMPENSATING', 'COMPENSATED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attempts_recovery_idx ON booking_attempts(created_at) WHERE state <> 'SUCCEEDED';
-- Durable cancellation intent: recovery finishes cancellation after a process crash.
CREATE TABLE cancellation_jobs (
    booking_id uuid PRIMARY KEY REFERENCES bookings(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
