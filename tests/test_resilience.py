import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import grpc
import pytest

from app.booking.client import CallDetails, CircuitBreaker, CircuitOpen, ResilienceInterceptor
from app.flight.service import AuthInterceptor
from app.generated import flight_pb2 as pb
from app.generated import flight_pb2_grpc as rpc


@pytest.mark.parametrize(
    "code,expected",
    [
        (grpc.StatusCode.UNAVAILABLE, 3),
        (grpc.StatusCode.DEADLINE_EXCEEDED, 3),
        (grpc.StatusCode.INVALID_ARGUMENT, 1),
        (grpc.StatusCode.NOT_FOUND, 1),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, 1),
        (grpc.StatusCode.FAILED_PRECONDITION, 1),
        (grpc.StatusCode.UNAUTHENTICATED, 1),
        (grpc.StatusCode.OK, 1),
    ],
)
def test_retries_only_transient_errors(code, expected):
    sleeps = []
    outcome = Mock()
    outcome.code.return_value = code
    call = Mock(return_value=outcome)
    client = ResilienceInterceptor("secret", CircuitBreaker(), sleep=sleeps.append)
    details = CallDetails("/test", None, None, None, False, None)
    assert client.intercept_unary_unary(call, details, pb.GetFlightRequest()) is outcome
    assert call.call_count == expected
    assert sleeps == ([0.1, 0.2] if expected == 3 else [])
    actual = call.call_args.args[0]
    assert actual.metadata == [("x-api-key", "secret")]
    assert actual.timeout == 2


def test_breaker_transitions_and_only_one_half_open_probe(caplog):
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, reset_timeout=10, clock=lambda: now[0])
    for _ in range(2):
        breaker.after(breaker.before(), True)
    assert breaker.state == "OPEN"
    with pytest.raises(CircuitOpen):
        breaker.before()
    now[0] = 10
    generation = breaker.before()
    assert breaker.state == "HALF_OPEN"
    with pytest.raises(CircuitOpen):
        breaker.before()
    breaker.after(generation, True)
    assert breaker.state == "OPEN"
    now[0] = 20
    generation = breaker.before()
    breaker.after(generation, False)
    assert breaker.state == "CLOSED"
    assert "CLOSED -> OPEN" in caplog.text
    assert "OPEN -> HALF_OPEN" in caplog.text
    assert "HALF_OPEN -> CLOSED" in caplog.text


def test_breaker_ignores_old_inflight_success_and_prunes_window():
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, window=5, clock=lambda: now[0])
    stale = breaker.before()
    breaker.after(breaker.before(), True)
    now[0] = 6
    breaker.after(breaker.before(), True)
    assert breaker.state == "CLOSED"
    breaker.after(breaker.before(), True)
    assert breaker.state == "OPEN"
    breaker.after(stale, False)
    assert breaker.state == "OPEN"


def test_open_circuit_does_not_invoke_transport():
    breaker = CircuitBreaker(threshold=1)
    breaker.after(breaker.before(), True)
    call = Mock()
    with pytest.raises(CircuitOpen):
        ResilienceInterceptor("secret", breaker).intercept_unary_unary(
            call,
            CallDetails("/test", None, None, None, False, None),
            pb.GetFlightRequest(),
        )
    call.assert_not_called()


def test_real_grpc_interceptor_retries_and_authenticates():
    class Unstable(rpc.FlightServiceServicer):
        def __init__(self):
            self.calls = 0

        def GetFlight(self, request, context):
            self.calls += 1
            if self.calls < 3:
                context.abort(grpc.StatusCode.UNAVAILABLE, "temporary")
            return pb.Flight(id=request.flight_id)

    service = Unstable()
    server = grpc.server(
        ThreadPoolExecutor(max_workers=2), interceptors=[AuthInterceptor("secret")]
    )
    rpc.add_FlightServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = rpc.FlightServiceStub(
                grpc.intercept_channel(
                    channel,
                    ResilienceInterceptor("secret", CircuitBreaker(), backoff=0.001),
                )
            )
            assert stub.GetFlight(pb.GetFlightRequest(flight_id="id")).id == "id"
            assert service.calls == 3
            with pytest.raises(grpc.RpcError) as err:
                rpc.FlightServiceStub(channel).GetFlight(pb.GetFlightRequest())
            assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
            assert service.calls == 3
    finally:
        server.stop(0).wait()


def test_half_open_concurrent_admission():
    now = [0.0]
    breaker = CircuitBreaker(threshold=1, reset_timeout=1, clock=lambda: now[0])
    breaker.after(breaker.before(), True)
    now[0] = 2
    barrier = threading.Barrier(8)

    def enter(_):
        barrier.wait()
        try:
            return breaker.before()
        except CircuitOpen:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(enter, range(8)))
    assert sum(result is not None for result in results) == 1
