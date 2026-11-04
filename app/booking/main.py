import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

import grpc
import psycopg
from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.booking.client import FlightClient
from app.booking.service import BookingService
from app.common.config import configure_logging, positive_float
from app.common.db import make_pool, migrate
from app.generated import flight_pb2 as pb

log = logging.getLogger(__name__)


class BookingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    flight_id: UUID
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: str = Field(min_length=3, max_length=254)
    seat_count: int = Field(ge=1, le=1000, strict=True)

    @field_validator("passenger_email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Invalid email address")
        return value


class BookingOutput(BaseModel):
    id: UUID
    user_id: str
    flight_id: UUID
    passenger_name: str
    passenger_email: str
    seat_count: int
    total_price_minor: int
    status: str
    created_at: datetime


def flight_json(flight):
    return {
        "id": flight.id,
        "flight_number": flight.flight_number,
        "airline": flight.airline,
        "origin": flight.origin,
        "destination": flight.destination,
        "departure_at": flight.departure_at.ToJsonString(),
        "arrival_at": flight.arrival_at.ToJsonString(),
        "total_seats": flight.total_seats,
        "available_seats": flight.available_seats,
        "price_minor": flight.price_minor,
        "currency": "RUB",
        "status": pb.FlightStatus.Name(flight.status),
    }


def create_app(service=None, flights=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.service = service
            app.state.flights = flights
            yield
            return
        configure_logging()
        pool = make_pool(os.environ["DATABASE_URL"])
        migrate(pool, "booking")
        client = FlightClient()
        booking = BookingService(
            pool,
            client.stub,
            positive_float("RECOVERY_STALE_SECONDS", 60),
        )
        app.state.service, app.state.flights = booking, client.stub
        stop = threading.Event()
        interval = positive_float("RECOVERY_INTERVAL_SECONDS", 5)

        def recovery_loop():
            while not stop.is_set():
                try:
                    booking.recover()
                except Exception:
                    log.exception("booking recovery iteration failed")
                stop.wait(interval)

        worker = threading.Thread(target=recovery_loop, name="booking-recovery", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=30)
            client.close()
            pool.close()

    app = FastAPI(title="Flight Booking API", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(grpc.RpcError)
    async def grpc_error(request, exc):
        mapping = {
            grpc.StatusCode.INVALID_ARGUMENT: 400,
            grpc.StatusCode.NOT_FOUND: 404,
            grpc.StatusCode.RESOURCE_EXHAUSTED: 409,
            grpc.StatusCode.FAILED_PRECONDITION: 409,
            grpc.StatusCode.ALREADY_EXISTS: 409,
            grpc.StatusCode.UNAUTHENTICATED: 502,
            grpc.StatusCode.PERMISSION_DENIED: 502,
            grpc.StatusCode.UNAVAILABLE: 503,
            grpc.StatusCode.DEADLINE_EXCEEDED: 504,
        }
        status = mapping.get(exc.code(), 502)
        headers = {"Retry-After": "15"} if status == 503 else None
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={"detail": exc.details(), "code": exc.code().name},
        )

    async def db_error(request, exc):
        log.error("booking database unavailable", exc_info=exc)
        return JSONResponse(status_code=503, content={"detail": "Booking database unavailable"})

    app.add_exception_handler(psycopg.OperationalError, db_error)
    app.add_exception_handler(PoolTimeout, db_error)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        with app.state.service.pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}

    @app.get("/flights")
    def search_flights(
        origin: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
        destination: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
        date: date | None = None,
    ):
        request = pb.SearchFlightsRequest(origin=origin, destination=destination)
        if date:
            request.departure_date.FromDatetime(datetime.combine(date, datetime.min.time(), UTC))
        result = app.state.flights.SearchFlights(request)
        return [flight_json(flight) for flight in result.flights]

    @app.get("/flights/{flight_id}")
    def get_flight(flight_id: UUID):
        return flight_json(
            app.state.flights.GetFlight(pb.GetFlightRequest(flight_id=str(flight_id)))
        )

    @app.post("/bookings", response_model=BookingOutput, status_code=201)
    def create_booking(
        data: BookingInput,
        idempotency_key: Annotated[
            UUID | None, Header(description="Optional UUID; retry with the same key and payload")
        ] = None,
    ):
        return app.state.service.create(data.model_dump(), idempotency_key)

    @app.get("/bookings/{booking_id}", response_model=BookingOutput)
    def get_booking(booking_id: UUID):
        return app.state.service.get(booking_id)

    @app.get("/bookings", response_model=list[BookingOutput])
    def list_bookings(user_id: Annotated[str, Query(min_length=1, max_length=128)]):
        return app.state.service.list(user_id)

    @app.post("/bookings/{booking_id}/cancel", response_model=BookingOutput)
    def cancel_booking(booking_id: UUID):
        return app.state.service.cancel(booking_id)

    return app


app = create_app()
