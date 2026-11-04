"""Auth, bounded retries and circuit breaker live in a gRPC client interceptor."""

import logging
import os
import threading
import time
from collections import namedtuple

import grpc

from app.common.config import positive_float, positive_int
from app.generated.flight_pb2_grpc import FlightServiceStub

log = logging.getLogger(__name__)
TRANSIENT = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}


class CircuitOpen(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "Flight Service circuit is OPEN; retry later"


class CircuitBreaker:
    def __init__(self, threshold=5, reset_timeout=15, window=30, clock=time.monotonic):
        self.threshold = threshold
        self.reset_timeout = reset_timeout
        self.window = window
        self.clock = clock
        self.state = "CLOSED"
        self.failures = []
        self.opened_at = 0
        self.generation = 0
        self.lock = threading.Lock()

    def transition(self, state):
        log.warning("circuit transition %s -> %s", self.state, state)
        self.state = state
        self.generation += 1

    def before(self):
        with self.lock:
            if self.state == "OPEN":
                if self.clock() - self.opened_at < self.reset_timeout:
                    raise CircuitOpen()
                self.transition("HALF_OPEN")
            elif self.state == "HALF_OPEN":
                raise CircuitOpen()
            return self.generation

    def after(self, generation, failed):
        with self.lock:
            # Ignore results of in-flight requests from an older breaker state.
            if generation != self.generation:
                return
            if not failed:
                self.failures.clear()
                if self.state == "HALF_OPEN":
                    self.transition("CLOSED")
                return
            now = self.clock()
            self.failures = [stamp for stamp in self.failures if now - stamp <= self.window]
            self.failures.append(now)
            if self.state == "HALF_OPEN" or len(self.failures) >= self.threshold:
                self.opened_at = now
                self.transition("OPEN")


CallDetails = namedtuple(
    "CallDetails", "method timeout metadata credentials wait_for_ready compression"
)


class ResilienceInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, api_key, breaker, attempts=3, backoff=0.1, timeout=2, sleep=time.sleep):
        if not api_key:
            raise ValueError("GRPC_API_KEY must not be empty")
        if not 1 <= attempts <= 3:
            raise ValueError("GRPC_MAX_ATTEMPTS must be between 1 and 3")
        self.api_key = api_key
        self.breaker = breaker
        self.attempts = attempts
        self.backoff = backoff
        self.timeout = timeout
        self.sleep = sleep

    def intercept_unary_unary(self, continuation, details, request):
        generation = self.breaker.before()
        metadata = list(details.metadata or []) + [("x-api-key", self.api_key)]
        call_details = CallDetails(
            details.method,
            details.timeout or self.timeout,
            metadata,
            details.credentials,
            details.wait_for_ready,
            details.compression,
        )
        try:
            for attempt in range(self.attempts):
                outcome = continuation(call_details, request)
                code = outcome.code()
                if code not in TRANSIENT or attempt == self.attempts - 1:
                    self.breaker.after(generation, code in TRANSIENT)
                    return outcome
                delay = self.backoff * 2**attempt
                log.warning(
                    "grpc retry method=%s code=%s attempt=%s delay=%.3f",
                    details.method,
                    code.name,
                    attempt + 2,
                    delay,
                )
                self.sleep(delay)
        except grpc.RpcError as exc:
            self.breaker.after(generation, exc.code() in TRANSIENT)
            raise
        except Exception:
            self.breaker.after(generation, True)
            raise


class FlightClient:
    def __init__(self):
        breaker = CircuitBreaker(
            positive_int("CB_FAILURE_THRESHOLD", 5),
            positive_float("CB_RESET_TIMEOUT_SECONDS", 15),
            positive_float("CB_WINDOW_SECONDS", 30),
        )
        interceptor = ResilienceInterceptor(
            os.environ["GRPC_API_KEY"],
            breaker,
            positive_int("GRPC_MAX_ATTEMPTS", 3),
            positive_float("GRPC_RETRY_BACKOFF_SECONDS", 0.1),
            positive_float("GRPC_TIMEOUT_SECONDS", 2),
        )
        self.channel = grpc.insecure_channel(
            os.getenv("FLIGHT_GRPC_TARGET", "flight-service:50051"),
            options=[("grpc.enable_retries", 0)],
        )
        self.stub = FlightServiceStub(grpc.intercept_channel(self.channel, interceptor))

    def close(self):
        self.channel.close()
