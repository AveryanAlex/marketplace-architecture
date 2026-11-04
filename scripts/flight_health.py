import os

import grpc

from app.generated import flight_pb2 as pb
from app.generated.flight_pb2_grpc import FlightServiceStub

with grpc.insecure_channel("localhost:50051") as channel:
    FlightServiceStub(channel).GetFlight(
        pb.GetFlightRequest(flight_id="11111111-1111-4111-8111-111111111111"),
        metadata=[("x-api-key", os.environ["GRPC_API_KEY"])],
        timeout=2,
    )
