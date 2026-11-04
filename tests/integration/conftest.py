import os
from uuid import uuid4

import grpc
import httpx
import pytest

from app.common.db import make_pool
from app.flight.cache import FlightCache, invalidate
from app.generated import flight_pb2_grpc as rpc


@pytest.fixture(scope="session")
def infrastructure():
    if not os.getenv("TEST_FLIGHT_DATABASE_URL"):
        pytest.skip("Run with docker compose --profile test run --rm tests")
    flight_pool = make_pool(os.environ["TEST_FLIGHT_DATABASE_URL"])
    booking_pool = make_pool(os.environ["TEST_BOOKING_DATABASE_URL"])
    channel = grpc.insecure_channel(os.getenv("TEST_FLIGHT_TARGET", "flight-service:50051"))
    api = httpx.Client(
        base_url=os.getenv("TEST_API_URL", "http://booking-service:8080"), timeout=30
    )
    value = {
        "flight_pool": flight_pool,
        "booking_pool": booking_pool,
        "stub": rpc.FlightServiceStub(channel),
        "api": api,
        "auth": [("x-api-key", os.environ["GRPC_API_KEY"])],
        "cache": FlightCache.from_env(),
    }
    yield value
    api.close()
    channel.close()
    booking_pool.close()
    flight_pool.close()


@pytest.fixture
def flight(infrastructure):
    flight_id = uuid4()
    with infrastructure["flight_pool"].connection() as conn:
        conn.execute(
            """INSERT INTO flights(id, flight_number, airline, origin, destination,
            departure_at, arrival_at, total_seats, available_seats, price_minor)
            VALUES (%s,%s,'Test Airline','SVO','LED',
            '2026-12-01T09:00Z','2026-12-01T11:00Z',3,3,12345)
            """,
            (flight_id, "T" + uuid4().hex[:10]),
        )
        invalidate(conn)
    yield str(flight_id)
    with infrastructure["flight_pool"].connection() as conn:
        ids = [
            row["booking_id"]
            for row in conn.execute(
                "SELECT booking_id FROM seat_reservations WHERE flight_id=%s", (flight_id,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM seat_reservations WHERE flight_id=%s", (flight_id,))
        conn.execute("DELETE FROM released_bookings WHERE booking_id=ANY(%s::uuid[])", (ids,))
        conn.execute("DELETE FROM flights WHERE id=%s", (flight_id,))
        invalidate(conn)
    with infrastructure["booking_pool"].connection() as conn:
        own = conn.execute("SELECT id FROM bookings WHERE flight_id=%s", (flight_id,)).fetchall()
        ids.extend(row["id"] for row in own)
        conn.execute("DELETE FROM cancellation_jobs WHERE booking_id=ANY(%s::uuid[])", (ids,))
        conn.execute("DELETE FROM bookings WHERE flight_id=%s", (flight_id,))
        conn.execute("DELETE FROM booking_attempts WHERE id=ANY(%s::uuid[])", (ids,))
