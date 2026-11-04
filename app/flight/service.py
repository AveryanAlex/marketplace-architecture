import functools
import hmac
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import grpc
import psycopg
from psycopg_pool import PoolTimeout

from app.flight.cache import invalidate
from app.generated import flight_pb2 as pb
from app.generated import flight_pb2_grpc as rpc

log = logging.getLogger(__name__)


class AuthInterceptor(grpc.ServerInterceptor):
    def __init__(self, api_key):
        if not api_key:
            raise ValueError("GRPC_API_KEY must not be empty")
        self.api_key = api_key

    def intercept_service(self, continuation, details):
        handler = continuation(details)
        credentials = dict(details.invocation_metadata).get("x-api-key", "")
        if hmac.compare_digest(credentials.encode(), self.api_key.encode()):
            return handler

        def reject(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid service API key")

        return grpc.unary_unary_rpc_method_handler(
            reject,
            request_deserializer=handler.request_deserializer if handler else None,
            response_serializer=handler.response_serializer if handler else None,
        )


def database_errors(method):
    @functools.wraps(method)
    def wrapped(self, request, context):
        try:
            return method(self, request, context)
        except (psycopg.OperationalError, PoolTimeout):
            log.exception("flight database unavailable")
            context.abort(grpc.StatusCode.UNAVAILABLE, "Flight database unavailable")
        except psycopg.Error:
            log.exception("flight database error")
            context.abort(grpc.StatusCode.INTERNAL, "Flight database error")

    return wrapped


def uuid_field(value, context, name):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"{name} must be a UUID")


def flight_message(row):
    result = pb.Flight(
        id=str(row["id"]),
        flight_number=row["flight_number"],
        airline=row["airline"],
        origin=row["origin"],
        destination=row["destination"],
        total_seats=row["total_seats"],
        available_seats=row["available_seats"],
        price_minor=row["price_minor"],
        status=pb.FlightStatus.Value(row["status"]),
    )
    result.departure_at.FromDatetime(row["departure_at"])
    result.arrival_at.FromDatetime(row["arrival_at"])
    return result


def reservation_message(row):
    result = pb.SeatReservation(
        id=str(row["id"]),
        flight_id=str(row["flight_id"]),
        booking_id=str(row["booking_id"]),
        seat_count=row["seat_count"],
        status=pb.ReservationStatus.Value(row["status"]),
    )
    result.created_at.FromDatetime(row["created_at"])
    return result


def lock_booking(conn, booking_id):
    # Reserve and Release lock the same key, including when no reservation exists yet.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(booking_id),))


class FlightService(rpc.FlightServiceServicer):
    def __init__(self, pool, cache):
        self.pool = pool
        self.cache = cache

    @database_errors
    def SearchFlights(self, request, context):
        if not all(
            re.fullmatch(r"[A-Z]{3}", code) for code in (request.origin, request.destination)
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "origin/destination must be IATA codes")
        if request.origin == request.destination:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "origin and destination must differ")
        day = None
        if request.HasField("departure_date"):
            try:
                day = request.departure_date.ToDatetime(tzinfo=UTC).date()
                if day == datetime.max.date():
                    raise ValueError("Date out of range")
            except (ValueError, OverflowError):
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid departure_date")
        with self.pool.connection() as conn:
            revision = conn.execute("SELECT revision FROM cache_revision WHERE id=1").fetchone()
            key = (
                f"search:{request.origin}:{request.destination}:{day or 'all'}"
                f":v{revision['revision']}"
            )
            cached = self.cache.get(key, pb.SearchFlightsResponse)
            if cached is not None:
                return cached
            query = """SELECT * FROM flights WHERE origin=%s AND destination=%s
                AND status='SCHEDULED'"""
            params = [request.origin, request.destination]
            if day:
                query += " AND departure_at >= %s AND departure_at < %s"
                params.extend(
                    [
                        datetime.combine(day, datetime.min.time(), UTC),
                        datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC),
                    ]
                )
            rows = conn.execute(query + " ORDER BY departure_at, id", params).fetchall()
            result = pb.SearchFlightsResponse(flights=[flight_message(row) for row in rows])
            self.cache.put(key, result)
            return result

    @database_errors
    def GetFlight(self, request, context):
        flight_id = uuid_field(request.flight_id, context, "flight_id")
        with self.pool.connection() as conn:
            revision = conn.execute("SELECT revision FROM cache_revision WHERE id=1").fetchone()
            key = f"flight:{flight_id}:v{revision['revision']}"
            cached = self.cache.get(key, pb.Flight)
            if cached is not None:
                return cached
            row = conn.execute("SELECT * FROM flights WHERE id=%s", (flight_id,)).fetchone()
            if row is None:
                context.abort(grpc.StatusCode.NOT_FOUND, "Flight not found")
            result = flight_message(row)
            self.cache.put(key, result)
            return result

    @database_errors
    def ReserveSeats(self, request, context):
        flight_id = uuid_field(request.flight_id, context, "flight_id")
        booking_id = uuid_field(request.booking_id, context, "booking_id")
        if not 1 <= request.seat_count <= 1000:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "seat_count must be between 1 and 1000")
        with self.pool.connection() as conn, conn.transaction():
            lock_booking(conn, booking_id)
            existing = conn.execute(
                "SELECT * FROM seat_reservations WHERE booking_id=%s", (booking_id,)
            ).fetchone()
            if existing:
                if (
                    existing["flight_id"] != flight_id
                    or existing["seat_count"] != request.seat_count
                ):
                    context.abort(grpc.StatusCode.ALREADY_EXISTS, "booking_id already used")
                if existing["status"] != "ACTIVE":
                    context.abort(
                        grpc.StatusCode.FAILED_PRECONDITION, "Reservation already released"
                    )
                return reservation_message(existing)
            if conn.execute(
                "SELECT 1 FROM released_bookings WHERE booking_id=%s", (booking_id,)
            ).fetchone():
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Booking already released")
            flight = conn.execute(
                "SELECT * FROM flights WHERE id=%s FOR UPDATE", (flight_id,)
            ).fetchone()
            if flight is None:
                context.abort(grpc.StatusCode.NOT_FOUND, "Flight not found")
            if flight["status"] != "SCHEDULED":
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Flight is not scheduled")
            if flight["available_seats"] < request.seat_count:
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Insufficient available seats")
            conn.execute(
                "UPDATE flights SET available_seats=available_seats-%s WHERE id=%s",
                (request.seat_count, flight_id),
            )
            row = conn.execute(
                """INSERT INTO seat_reservations(id, booking_id, flight_id, seat_count)
                VALUES (%s, %s, %s, %s) RETURNING *""",
                (uuid4(), booking_id, flight_id, request.seat_count),
            ).fetchone()
            invalidate(conn)
        return reservation_message(row)

    @database_errors
    def ReleaseReservation(self, request, context):
        booking_id = uuid_field(request.booking_id, context, "booking_id")
        with self.pool.connection() as conn, conn.transaction():
            lock_booking(conn, booking_id)
            conn.execute(
                "INSERT INTO released_bookings(booking_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (booking_id,),
            )
            row = conn.execute(
                "SELECT * FROM seat_reservations WHERE booking_id=%s FOR UPDATE", (booking_id,)
            ).fetchone()
            if row and row["status"] == "ACTIVE":
                conn.execute(
                    "UPDATE flights SET available_seats=available_seats+%s WHERE id=%s",
                    (row["seat_count"], row["flight_id"]),
                )
                conn.execute(
                    "UPDATE seat_reservations SET status='RELEASED' WHERE booking_id=%s",
                    (booking_id,),
                )
                invalidate(conn)
        return pb.ReleaseReservationResponse(booking_id=str(booking_id), status=pb.RELEASED)

    @database_errors
    def UpdateFlight(self, request, context):
        flight_id = uuid_field(request.flight_id, context, "flight_id")
        if request.HasField("price_minor") and not 1 <= request.price_minor <= 10**12:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid price_minor")
        if request.HasField("total_seats") and request.total_seats <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "total_seats must be positive")
        if request.HasField("status") and request.status not in (
            pb.SCHEDULED,
            pb.DEPARTED,
            pb.CANCELLED,
            pb.COMPLETED,
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flight status")
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM flights WHERE id=%s FOR UPDATE", (flight_id,)
            ).fetchone()
            if row is None:
                context.abort(grpc.StatusCode.NOT_FOUND, "Flight not found")
            reserved = row["total_seats"] - row["available_seats"]
            total = request.total_seats if request.HasField("total_seats") else row["total_seats"]
            if total < reserved:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Capacity below reserved seats")
            row = conn.execute(
                """UPDATE flights SET total_seats=%s, available_seats=%s,
                price_minor=%s, status=%s WHERE id=%s RETURNING *""",
                (
                    total,
                    total - reserved,
                    request.price_minor if request.HasField("price_minor") else row["price_minor"],
                    pb.FlightStatus.Name(request.status)
                    if request.HasField("status")
                    else row["status"],
                    flight_id,
                ),
            ).fetchone()
            invalidate(conn)
        return flight_message(row)
