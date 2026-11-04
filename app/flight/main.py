import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor

import grpc

from app.common.config import configure_logging, positive_int
from app.common.db import make_pool, migrate
from app.flight.cache import FlightCache
from app.flight.service import AuthInterceptor, FlightService
from app.generated.flight_pb2_grpc import add_FlightServiceServicer_to_server


def main():
    configure_logging()
    pool = make_pool(os.environ["DATABASE_URL"])
    migrate(pool, "flight")
    server = grpc.server(
        ThreadPoolExecutor(max_workers=positive_int("GRPC_WORKERS", 16)),
        interceptors=[AuthInterceptor(os.environ["GRPC_API_KEY"])],
    )
    add_FlightServiceServicer_to_server(FlightService(pool, FlightCache.from_env()), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    logging.info("Flight Service listening on :50051")
    signal.signal(signal.SIGTERM, lambda *_: server.stop(5))
    signal.signal(signal.SIGINT, lambda *_: server.stop(5))
    try:
        server.wait_for_termination()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
