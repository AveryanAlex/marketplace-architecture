import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import grpc
import pytest
from psycopg import sql

from app.booking.service import BookingService
from app.common.db import migrate
from app.generated import flight_pb2 as pb

pytestmark = pytest.mark.integration


def payload(flight, seats=1):
    return {
        "user_id": "test-" + uuid4().hex,
        "flight_id": flight,
        "passenger_name": "Иван Тестов",
        "passenger_email": "ivan@example.com",
        "seat_count": seats,
    }


def get_flight(infra, flight):
    return infra["stub"].GetFlight(pb.GetFlightRequest(flight_id=flight), metadata=infra["auth"])


def reserve(infra, flight, booking=None, seats=1):
    return infra["stub"].ReserveSeats(
        pb.ReserveSeatsRequest(
            flight_id=flight,
            booking_id=str(booking or uuid4()),
            seat_count=seats,
        ),
        metadata=infra["auth"],
    )


def release(infra, booking):
    return infra["stub"].ReleaseReservation(
        pb.ReleaseReservationRequest(booking_id=str(booking)),
        metadata=infra["auth"],
    )


class AuthenticatedStub:
    def __init__(self, infra):
        self.infra = infra

    def __getattr__(self, name):
        method = getattr(self.infra["stub"], name)
        return lambda request: method(request, metadata=self.infra["auth"])


def test_rest_booking_snapshot_replay_and_cancellation(infrastructure, flight):
    api = infrastructure["api"]
    data = payload(flight, seats=2)
    key = str(uuid4())
    response = api.post("/bookings", json=data, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    booking = response.json()
    assert booking["total_price_minor"] == 24690
    assert booking["status"] == "CONFIRMED"
    assert get_flight(infrastructure, flight).available_seats == 1
    replay = api.post("/bookings", json=data, headers={"Idempotency-Key": key})
    assert replay.status_code == 201 and replay.json() == booking
    conflict = api.post(
        "/bookings", json={**data, "seat_count": 1}, headers={"Idempotency-Key": key}
    )
    assert conflict.status_code == 409
    infrastructure["stub"].UpdateFlight(
        pb.UpdateFlightRequest(flight_id=flight, price_minor=99999),
        metadata=infrastructure["auth"],
    )
    assert api.get(f"/bookings/{key}").json()["total_price_minor"] == 24690
    assert booking in api.get("/bookings", params={"user_id": data["user_id"]}).json()
    assert api.post(f"/bookings/{key}/cancel").json()["status"] == "CANCELLED"
    assert api.post(f"/bookings/{key}/cancel").status_code == 200
    assert get_flight(infrastructure, flight).available_seats == 3
    assert api.get(f"/bookings/{uuid4()}").status_code == 404
    assert api.get(f"/flights/{uuid4()}").status_code == 404


def test_failed_reservation_creates_no_booking(infrastructure, flight):
    data = payload(flight, 4)
    key = str(uuid4())
    response = infrastructure["api"].post("/bookings", json=data, headers={"Idempotency-Key": key})
    assert response.status_code == 409, response.text
    assert get_flight(infrastructure, flight).available_seats == 3
    assert infrastructure["api"].get(f"/bookings/{key}").status_code == 404
    with infrastructure["flight_pool"].connection() as conn:
        assert (
            conn.execute("SELECT 1 FROM seat_reservations WHERE booking_id=%s", (key,)).fetchone()
            is None
        )
        conn.execute("DELETE FROM released_bookings WHERE booking_id=%s", (key,))
    with infrastructure["booking_pool"].connection() as conn:
        conn.execute("DELETE FROM booking_attempts WHERE id=%s", (key,))


def test_last_seat_concurrency_and_transaction_integrity(infrastructure, flight):
    infrastructure["stub"].UpdateFlight(
        pb.UpdateFlightRequest(flight_id=flight, total_seats=1), metadata=infrastructure["auth"]
    )

    def attempt(_):
        try:
            return reserve(infrastructure, flight).status
        except grpc.RpcError as exc:
            return exc.code()

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(attempt, range(12)))
    assert results.count(pb.ACTIVE) == 1
    assert results.count(grpc.StatusCode.RESOURCE_EXHAUSTED) == 11
    assert get_flight(infrastructure, flight).available_seats == 0
    with infrastructure["flight_pool"].connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM seat_reservations WHERE flight_id=%s", (flight,)
            ).fetchone()["n"]
            == 1
        )


def test_concurrent_duplicate_reserve_and_release(infrastructure, flight):
    booking = uuid4()
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _: reserve(infrastructure, flight, booking), range(8)))
    assert len({row.id for row in rows}) == 1
    assert get_flight(infrastructure, flight).available_seats == 2
    with pytest.raises(grpc.RpcError) as err:
        reserve(infrastructure, flight, booking, seats=2)
    assert err.value.code() == grpc.StatusCode.ALREADY_EXISTS
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: release(infrastructure, booking), range(8)))
    assert get_flight(infrastructure, flight).available_seats == 3
    with pytest.raises(grpc.RpcError) as err:
        reserve(infrastructure, flight, booking)
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_compensation_tombstone_prevents_delayed_reserve(infrastructure, flight):
    booking = uuid4()
    release(infrastructure, booking)
    with pytest.raises(grpc.RpcError) as err:
        reserve(infrastructure, flight, booking)
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert get_flight(infrastructure, flight).available_seats == 3
    with infrastructure["flight_pool"].connection() as conn:
        conn.execute("DELETE FROM released_bookings WHERE booking_id=%s", (booking,))


@pytest.mark.parametrize(
    "method,message",
    [
        ("GetFlight", pb.GetFlightRequest()),
        ("SearchFlights", pb.SearchFlightsRequest()),
        ("ReserveSeats", pb.ReserveSeatsRequest()),
        ("ReleaseReservation", pb.ReleaseReservationRequest()),
        ("UpdateFlight", pb.UpdateFlightRequest()),
    ],
)
@pytest.mark.parametrize("metadata", [[], [("x-api-key", "wrong")]])
def test_authentication_covers_every_rpc(infrastructure, method, message, metadata):
    with pytest.raises(grpc.RpcError) as err:
        getattr(infrastructure["stub"], method)(message, metadata=metadata)
    assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_cache_search_and_mutations(infrastructure, flight):
    api = infrastructure["api"]
    redis = infrastructure["cache"].client
    params = {"origin": "SVO", "destination": "LED", "date": "2026-12-01"}
    before = api.get("/flights", params=params)
    assert before.status_code == 200
    assert next(row for row in before.json() if row["id"] == flight)["available_seats"] == 3
    assert get_flight(infrastructure, flight).available_seats == 3
    reservation = reserve(infrastructure, flight)
    assert get_flight(infrastructure, flight).available_seats == 2
    after = api.get("/flights", params=params).json()
    assert next(row for row in after if row["id"] == flight)["available_seats"] == 2
    release(infrastructure, reservation.booking_id)
    assert get_flight(infrastructure, flight).available_seats == 3
    infrastructure["stub"].UpdateFlight(
        pb.UpdateFlightRequest(flight_id=flight, status=pb.CANCELLED),
        metadata=infrastructure["auth"],
    )
    assert flight not in {row["id"] for row in api.get("/flights", params=params).json()}
    assert get_flight(infrastructure, flight).status == pb.CANCELLED
    keys = list(redis.scan_iter("flight:*")) + list(redis.scan_iter("search:*"))
    assert keys
    assert all(0 < redis.ttl(key) <= 300 for key in keys)
    assert len(api.get("/flights", params={"origin": "SVO", "destination": "LED"}).json()) >= 2


def test_lost_reserve_reply_is_compensated(infrastructure, flight):
    real = AuthenticatedStub(infrastructure)

    class LostReply:
        GetFlight = staticmethod(real.GetFlight)
        ReleaseReservation = staticmethod(real.ReleaseReservation)

        def ReserveSeats(self, request):
            real.ReserveSeats(request)
            raise SimulatedUnavailable()

    booking = uuid4()
    service = BookingService(infrastructure["booking_pool"], LostReply())
    with pytest.raises(SimulatedUnavailable):
        service.create(payload(flight), booking)
    assert get_flight(infrastructure, flight).available_seats == 3
    with infrastructure["booking_pool"].connection() as conn:
        assert conn.execute("SELECT 1 FROM bookings WHERE id=%s", (booking,)).fetchone() is None
        assert (
            conn.execute("SELECT state FROM booking_attempts WHERE id=%s", (booking,)).fetchone()[
                "state"
            ]
            == "COMPENSATED"
        )


class SimulatedUnavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "Simulated lost response"


def test_database_insert_failure_compensates_seats(infrastructure, flight):
    booking = uuid4()
    # Inject a real PostgreSQL failure, scoped to this one booking UUID.
    name = "reject_" + booking.hex
    with infrastructure["booking_pool"].connection() as conn:
        conn.execute(
            sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.id = {}::uuid THEN RAISE EXCEPTION 'injected failure'; END IF;
            RETURN NEW; END $$""").format(sql.Identifier(name), sql.Literal(str(booking)))
        )
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON bookings FOR EACH ROW EXECUTE FUNCTION {}()"
            ).format(sql.Identifier(name), sql.Identifier(name))
        )
    try:
        service = BookingService(infrastructure["booking_pool"], AuthenticatedStub(infrastructure))
        with pytest.raises(Exception, match="injected failure"):
            service.create(payload(flight), booking)
        assert get_flight(infrastructure, flight).available_seats == 3
        with infrastructure["booking_pool"].connection() as conn:
            assert conn.execute("SELECT 1 FROM bookings WHERE id=%s", (booking,)).fetchone() is None
    finally:
        with infrastructure["booking_pool"].connection() as conn:
            conn.execute(sql.SQL("DROP TRIGGER {} ON bookings").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))


def test_recovery_after_crash_and_pending_cancellation(infrastructure, flight):
    pool = infrastructure["booking_pool"]
    service = BookingService(pool, AuthenticatedStub(infrastructure), recovery_age=0)
    orphan = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO booking_attempts(id, fingerprint) VALUES (%s, 'crash')", (orphan,)
        )
    reserve(infrastructure, flight, orphan)
    assert get_flight(infrastructure, flight).available_seats == 2
    service.recover()
    assert get_flight(infrastructure, flight).available_seats == 3
    booking = service.create(payload(flight))
    with pool.connection() as conn:
        conn.execute("INSERT INTO cancellation_jobs(booking_id) VALUES (%s)", (booking["id"],))
    # Emulate crash between successful release and local status update.
    release(infrastructure, booking["id"])
    service.recover()
    assert service.get(booking["id"])["status"] == "CANCELLED"
    assert get_flight(infrastructure, flight).available_seats == 3


def test_migrations_are_repeatable(infrastructure):
    migrate(infrastructure["flight_pool"], "flight")
    migrate(infrastructure["booking_pool"], "booking")


def test_redis_outage_cannot_resurrect_stale_cache(infrastructure, flight):
    from unittest.mock import Mock

    from redis.exceptions import ConnectionError

    from app.flight.cache import FlightCache
    from app.flight.service import FlightService

    # Warm Redis, mutate while cache writes fail, reconnect using the original cache.
    assert get_flight(infrastructure, flight).available_seats == 3
    broken = Mock()
    broken.get.side_effect = ConnectionError("offline")
    broken.set.side_effect = ConnectionError("offline")
    local = FlightService(infrastructure["flight_pool"], FlightCache(broken))
    request = pb.GetFlightRequest(flight_id=flight)
    assert local.GetFlight(request, Mock()).available_seats == 3
    local.ReserveSeats(
        pb.ReserveSeatsRequest(flight_id=flight, booking_id=str(uuid4()), seat_count=1), Mock()
    )
    assert get_flight(infrastructure, flight).available_seats == 2


def test_price_snapshot_fingerprint_is_stable(infrastructure, flight):
    data = payload(flight)
    data["flight_id"] = UUID(flight)
    booking = uuid4()
    service = BookingService(infrastructure["booking_pool"], AuthenticatedStub(infrastructure))
    result = service.create(data, booking)
    expected = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    with infrastructure["booking_pool"].connection() as conn:
        assert (
            conn.execute(
                "SELECT fingerprint FROM booking_attempts WHERE id=%s", (booking,)
            ).fetchone()["fingerprint"]
            == expected
        )
    assert service.create(data, booking)["id"] == result["id"]


def test_business_errors_and_capacity_checks(infrastructure, flight):
    auth = infrastructure["auth"]
    stub = infrastructure["stub"]
    with pytest.raises(grpc.RpcError) as err:
        stub.GetFlight(pb.GetFlightRequest(flight_id="bad"), metadata=auth)
    assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.RpcError) as err:
        stub.GetFlight(pb.GetFlightRequest(flight_id=str(uuid4())), metadata=auth)
    assert err.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.RpcError) as err:
        reserve(infrastructure, flight, seats=0)
    assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    booking = uuid4()
    reserve(infrastructure, flight, booking, seats=2)
    with pytest.raises(grpc.RpcError) as err:
        stub.UpdateFlight(pb.UpdateFlightRequest(flight_id=flight, total_seats=1), metadata=auth)
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert get_flight(infrastructure, flight).total_seats == 3
    stub.UpdateFlight(pb.UpdateFlightRequest(flight_id=flight, status=pb.DEPARTED), metadata=auth)
    with pytest.raises(grpc.RpcError) as err:
        reserve(infrastructure, flight)
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert get_flight(infrastructure, flight).available_seats == 1


def test_failed_compensation_survives_restart(infrastructure, flight):
    real = AuthenticatedStub(infrastructure)

    class OfflineAfterReserve:
        GetFlight = staticmethod(real.GetFlight)

        def ReserveSeats(self, request):
            real.ReserveSeats(request)
            raise SimulatedUnavailable()

        def ReleaseReservation(self, request):
            raise SimulatedUnavailable()

    booking = uuid4()
    service = BookingService(infrastructure["booking_pool"], OfflineAfterReserve())
    with pytest.raises(SimulatedUnavailable):
        service.create(payload(flight), booking)
    assert get_flight(infrastructure, flight).available_seats == 2
    with infrastructure["booking_pool"].connection() as conn:
        assert (
            conn.execute("SELECT state FROM booking_attempts WHERE id=%s", (booking,)).fetchone()[
                "state"
            ]
            == "COMPENSATING"
        )
        assert conn.execute("SELECT 1 FROM bookings WHERE id=%s", (booking,)).fetchone() is None
    # A new service instance reads durable intents and completes compensation.
    restarted = BookingService(infrastructure["booking_pool"], real)
    restarted.recover()
    assert get_flight(infrastructure, flight).available_seats == 3


@pytest.mark.parametrize(
    "assignment",
    [
        "available_seats=-1",
        "available_seats=total_seats+1",
        "total_seats=0",
        "price_minor=0",
        "origin='bad'",
        "destination=origin",
        "arrival_at=departure_at",
    ],
)
def test_database_integrity_checks(infrastructure, flight, assignment):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with infrastructure["flight_pool"].connection() as conn:
            conn.execute(
                sql.SQL("UPDATE flights SET {} WHERE id=%s").format(sql.SQL(assignment)), (flight,)
            )
    assert get_flight(infrastructure, flight).available_seats == 3


def test_flight_number_is_unique_per_utc_day(infrastructure, flight):
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):
        with infrastructure["flight_pool"].connection() as conn:
            conn.execute(
                """INSERT INTO flights(id,flight_number,airline,origin,destination,
                departure_at,arrival_at,total_seats,available_seats,price_minor)
                SELECT %s,flight_number,airline,origin,destination,
                departure_at+interval '1 hour',arrival_at+interval '1 hour',
                total_seats,available_seats,price_minor FROM flights WHERE id=%s""",
                (uuid4(), flight),
            )
