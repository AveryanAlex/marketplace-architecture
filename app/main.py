"""Minimal HTTP service used to verify service initialization.

There is deliberately no marketplace business logic here: this iteration only
proves that a container can start and expose a health endpoint.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    """Serve the liveness endpoint and reject every other route."""

    server_version = "marketplace-gateway/0.1"

    def do_GET(self) -> None:  # noqa: N802 - name is defined by BaseHTTPRequestHandler
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Keep the standard access log but send it through one predictable line.
        print(f'{self.address_string()} - {format % args}', flush=True)


def create_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """Create a server separately so the handler can be tested without a process."""

    return ThreadingHTTPServer((host, port), HealthHandler)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = create_server(port=port)
    print(f"marketplace-gateway listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
