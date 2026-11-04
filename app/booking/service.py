import hashlib
import json
import logging
from uuid import uuid4

from fastapi import HTTPException

from app.generated import flight_pb2 as pb

log = logging.getLogger(__name__)


class BookingService:
    def __init__(self, pool, flights, recovery_age=60):
        self.pool = pool
        self.flights = flights
        self.recovery_age = recovery_age

    def get(self, booking_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id=%s", (booking_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Booking not found")
        return row

    def list(self, user_id):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM bookings WHERE user_id=%s ORDER BY created_at DESC, id", (user_id,)
            ).fetchall()

    @staticmethod
    def replay(conn, attempt, fingerprint):
        if attempt["fingerprint"] != fingerprint:
            raise HTTPException(409, "Idempotency-Key already used with another request")
        if attempt["state"] == "SUCCEEDED":
            return conn.execute("SELECT * FROM bookings WHERE id=%s", (attempt["id"],)).fetchone()
        if attempt["state"] != "PROCESSING":
            raise HTTPException(409, "Previous attempt failed; use a new Idempotency-Key")
        return None

    def create(self, data, booking_id=None):
        booking_id = booking_id or uuid4()
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()
        with self.pool.connection() as conn:
            attempt = conn.execute(
                "SELECT * FROM booking_attempts WHERE id=%s", (booking_id,)
            ).fetchone()
            if attempt:
                booking = self.replay(conn, attempt, fingerprint)
                if booking:
                    return booking
        # Required order: GetFlight -> ReserveSeats -> immutable price snapshot + booking.
        flight = self.flights.GetFlight(pb.GetFlightRequest(flight_id=str(data["flight_id"])))
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO booking_attempts(id, fingerprint) VALUES (%s, %s)
                ON CONFLICT DO NOTHING""",
                (booking_id, fingerprint),
            )
        try:
            with self.pool.connection() as conn, conn.transaction():
                attempt = conn.execute(
                    "SELECT * FROM booking_attempts WHERE id=%s FOR UPDATE", (booking_id,)
                ).fetchone()
                booking = self.replay(conn, attempt, fingerprint)
                if booking:
                    return booking
                self.flights.ReserveSeats(
                    pb.ReserveSeatsRequest(
                        flight_id=str(data["flight_id"]),
                        booking_id=str(booking_id),
                        seat_count=data["seat_count"],
                    )
                )
                row = conn.execute(
                    """INSERT INTO bookings
                    (id, user_id, flight_id, passenger_name, passenger_email, seat_count,
                     total_price_minor) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        booking_id,
                        data["user_id"],
                        data["flight_id"],
                        data["passenger_name"],
                        data["passenger_email"],
                        data["seat_count"],
                        data["seat_count"] * flight.price_minor,
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE booking_attempts SET state='SUCCEEDED' WHERE id=%s", (booking_id,)
                )
            return row
        except HTTPException:
            # A conflicting replay must not compensate another request's reservation.
            raise
        except Exception:
            # Commit outcome can be unknown. Re-read durable state before compensating;
            # a successfully committed booking must never have its seats released here.
            try:
                with self.pool.connection() as conn:
                    conn.execute(
                        """UPDATE booking_attempts SET state='COMPENSATING'
                        WHERE id=%s AND state='PROCESSING'""",
                        (booking_id,),
                    )
                self.compensate(booking_id)
            except Exception:
                log.exception("compensation deferred to recovery worker booking_id=%s", booking_id)
            raise

    def compensate(self, booking_id, recovery=False):
        with self.pool.connection() as conn, conn.transaction():
            attempt = conn.execute(
                "SELECT * FROM booking_attempts WHERE id=%s FOR UPDATE SKIP LOCKED", (booking_id,)
            ).fetchone()
            if not attempt or attempt["state"] in ("SUCCEEDED", "COMPENSATED"):
                return
            if recovery and attempt["state"] == "PROCESSING":
                stale = conn.execute(
                    """SELECT 1 FROM booking_attempts WHERE id=%s
                    AND created_at < now() - (%s * interval '1 second')""",
                    (booking_id, self.recovery_age),
                ).fetchone()
                if not stale:
                    return
            self.flights.ReleaseReservation(
                pb.ReleaseReservationRequest(booking_id=str(booking_id))
            )
            conn.execute(
                "UPDATE booking_attempts SET state='COMPENSATED' WHERE id=%s", (booking_id,)
            )
            log.info("booking attempt compensated booking_id=%s", booking_id)

    def cancel(self, booking_id):
        # Commit intent first: a crash after ReleaseReservation is recoverable.
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM bookings WHERE id=%s FOR UPDATE", (booking_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Booking not found")
            if row["status"] == "CANCELLED":
                return row
            conn.execute(
                """INSERT INTO cancellation_jobs(booking_id) VALUES (%s)
                ON CONFLICT DO NOTHING""",
                (booking_id,),
            )
        return self.finish_cancel(booking_id)

    def finish_cancel(self, booking_id, recovery=False):
        with self.pool.connection() as conn, conn.transaction():
            query = "SELECT * FROM bookings WHERE id=%s FOR UPDATE"
            if recovery:
                query += " SKIP LOCKED"
            row = conn.execute(query, (booking_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "CONFIRMED":
                self.flights.ReleaseReservation(
                    pb.ReleaseReservationRequest(booking_id=str(booking_id))
                )
                row = conn.execute(
                    """UPDATE bookings SET status='CANCELLED'
                    WHERE id=%s RETURNING *""",
                    (booking_id,),
                ).fetchone()
            conn.execute("DELETE FROM cancellation_jobs WHERE booking_id=%s", (booking_id,))
            log.info("booking cancelled booking_id=%s", booking_id)
            return row

    def recover(self):
        with self.pool.connection() as conn:
            attempts = conn.execute(
                """SELECT id FROM booking_attempts
                WHERE state='COMPENSATING' OR (state='PROCESSING'
                AND created_at < now() - (%s * interval '1 second'))
                ORDER BY created_at LIMIT 100""",
                (self.recovery_age,),
            ).fetchall()
            cancellations = conn.execute(
                "SELECT booking_id FROM cancellation_jobs ORDER BY created_at LIMIT 100"
            ).fetchall()
        for row in attempts:
            try:
                self.compensate(row["id"], recovery=True)
            except Exception:
                log.warning("recovery compensation pending booking_id=%s", row["id"], exc_info=True)
                break
        for row in cancellations:
            try:
                self.finish_cancel(row["booking_id"], recovery=True)
            except Exception:
                log.warning(
                    "recovery cancellation pending booking_id=%s", row["booking_id"], exc_info=True
                )
                break
