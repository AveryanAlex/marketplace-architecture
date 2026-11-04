from unittest.mock import Mock

import grpc
from fastapi.testclient import TestClient

from app.booking.client import CircuitOpen
from app.booking.main import create_app
from app.generated import flight_pb2 as pb


def test_api_validation_and_open_circuit_error():
    flights = Mock()
    flights.GetFlight.side_effect = CircuitOpen()
    with TestClient(create_app(service=Mock(), flights=flights)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/flights").status_code == 422
        assert client.get("/flights/not-a-uuid").status_code == 422
        response = client.get("/flights/11111111-1111-4111-8111-111111111111")
        assert response.status_code == 503
        assert "OPEN" in response.json()["detail"]
        invalid = client.post(
            "/bookings",
            json={
                "user_id": "x",
                "flight_id": "11111111-1111-4111-8111-111111111111",
                "passenger_name": " ",
                "passenger_email": "invalid",
                "seat_count": -1,
            },
        )
        assert invalid.status_code == 422


def test_optional_search_date_and_status_serialization():
    flights = Mock()
    flights.SearchFlights.return_value = pb.SearchFlightsResponse(
        flights=[
            pb.Flight(id="id", status=pb.SCHEDULED, price_minor=100),
        ]
    )
    with TestClient(create_app(service=Mock(), flights=flights)) as client:
        response = client.get("/flights?origin=SVO&destination=LED&date=2026-12-01")
        assert response.status_code == 200
        assert response.json()[0]["status"] == "SCHEDULED"
        assert response.json()[0]["currency"] == "RUB"
        request = flights.SearchFlights.call_args.args[0]
        assert request.departure_date.ToJsonString() == "2026-12-01T00:00:00Z"
        assert client.get("/flights?origin=SVO&destination=LED").status_code == 200
        assert not flights.SearchFlights.call_args.args[0].HasField("departure_date")


def test_business_error_mapping():
    class Insufficient(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.RESOURCE_EXHAUSTED

        def details(self):
            return "Insufficient seats"

    service = Mock()
    service.create.side_effect = Insufficient()
    with TestClient(create_app(service=service, flights=Mock())) as client:
        result = client.post(
            "/bookings",
            json={
                "user_id": "x",
                "flight_id": "11111111-1111-4111-8111-111111111111",
                "passenger_name": "Ivan",
                "passenger_email": "ivan@example.com",
                "seat_count": 1,
            },
        )
        assert result.status_code == 409
        assert result.json()["code"] == "RESOURCE_EXHAUSTED"
