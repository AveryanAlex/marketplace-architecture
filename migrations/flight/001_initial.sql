CREATE TYPE flight_status AS ENUM ('SCHEDULED', 'DEPARTED', 'CANCELLED', 'COMPLETED');
CREATE TYPE reservation_status AS ENUM ('ACTIVE', 'RELEASED', 'EXPIRED');
CREATE TABLE flights (
    id uuid PRIMARY KEY,
    flight_number text NOT NULL CHECK (length(flight_number) BETWEEN 2 AND 12),
    airline text NOT NULL CHECK (length(airline) > 0),
    origin char(3) NOT NULL CHECK (origin ~ '^[A-Z]{3}$'),
    destination char(3) NOT NULL CHECK (destination ~ '^[A-Z]{3}$' AND destination <> origin),
    departure_at timestamptz NOT NULL,
    arrival_at timestamptz NOT NULL CHECK (arrival_at > departure_at),
    departure_date date GENERATED ALWAYS AS ((departure_at AT TIME ZONE 'UTC')::date) STORED,
    total_seats integer NOT NULL CHECK (total_seats > 0),
    available_seats integer NOT NULL CHECK (available_seats BETWEEN 0 AND total_seats),
    price_minor bigint NOT NULL CHECK (price_minor BETWEEN 1 AND 1000000000000),
    status flight_status NOT NULL DEFAULT 'SCHEDULED',
    UNIQUE (flight_number, departure_date)
);
CREATE INDEX flights_search_idx ON flights (origin, destination, departure_date)
    WHERE status = 'SCHEDULED';
CREATE TABLE seat_reservations (
    id uuid PRIMARY KEY,
    booking_id uuid NOT NULL UNIQUE,
    flight_id uuid NOT NULL REFERENCES flights(id),
    seat_count integer NOT NULL CHECK (seat_count BETWEEN 1 AND 1000),
    status reservation_status NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reservations_flight_idx ON seat_reservations(flight_id) WHERE status = 'ACTIVE';
-- Independent tombstones also cover ReleaseReservation arriving before ReserveSeats.
CREATE TABLE released_bookings (
    booking_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Transactional cache namespace: old keys are unreachable even after Redis failover.
CREATE TABLE cache_revision (
    id integer PRIMARY KEY CHECK (id = 1),
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO cache_revision (id) VALUES (1);
