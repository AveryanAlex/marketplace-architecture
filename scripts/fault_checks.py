"""Exercise real Sentinel failover and circuit recovery on the local Compose stack."""

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    return subprocess.check_output(["docker", "compose", *args], cwd=ROOT, text=True).strip()


def redis(service, *args, port=6379):
    return compose("exec", "-T", service, "redis-cli", "--raw", "-p", str(port), *args)


def wait_until(check, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {description}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    args = parser.parse_args()
    stopped = set()
    with httpx.Client(base_url=args.base_url, timeout=15) as api:
        assert api.get("/flights/11111111-1111-4111-8111-111111111111").status_code == 200
        current = next(
            service
            for service in ("redis-master", "redis-replica")
            if redis(service, "ROLE").splitlines()[0] == "master"
        )
        other = "redis-replica" if current == "redis-master" else "redis-master"
        try:
            print(f"Stopping Redis master: {current}", flush=True)
            compose("stop", "-t", "2", current)
            stopped.add(current)
            assert api.get("/flights/11111111-1111-4111-8111-111111111111").status_code == 200
            wait_until(
                lambda: redis(other, "ROLE").splitlines()[0] == "master", 45, "Sentinel promotion"
            )
            wait_until(
                lambda: api.get("/flights/11111111-1111-4111-8111-111111111111").status_code == 200,
                10,
                "API after failover",
            )
            # Prove that the application's Sentinel-aware client writes to the promoted master.
            result = compose(
                "exec",
                "-T",
                "flight-service",
                "python",
                "-c",
                "from app.flight.cache import FlightCache; "
                "c=FlightCache.from_env().client; "
                "c.set('failover:probe', 'ok', ex=300); print(c.get('failover:probe'))",
            )
            assert "ok" in result
            assert redis(other, "GET", "failover:probe") == "ok"
            print(f"Sentinel promoted {other}; API and Sentinel client work", flush=True)
            compose("start", current)
            stopped.remove(current)

            config = json.loads(compose("config", "--format", "json"))
            env = config["services"]["booking-service"]["environment"]
            threshold = int(env["CB_FAILURE_THRESHOLD"])
            reset = float(env["CB_RESET_TIMEOUT_SECONDS"])
            print("Stopping Flight Service", flush=True)
            compose("stop", "-t", "2", "flight-service")
            stopped.add("flight-service")
            for _ in range(threshold):
                response = api.get("/flights/11111111-1111-4111-8111-111111111111")
                assert response.status_code in (503, 504), response.text
            start = time.monotonic()
            response = api.get("/flights/11111111-1111-4111-8111-111111111111")
            elapsed = time.monotonic() - start
            assert response.status_code == 503 and "OPEN" in response.json()["detail"]
            assert elapsed < 1, elapsed
            print(f"Circuit OPEN rejects immediately ({elapsed:.3f}s)", flush=True)
            compose("start", "flight-service")
            stopped.remove("flight-service")
            wait_until(
                lambda: api.get("/flights/11111111-1111-4111-8111-111111111111").status_code == 200,
                reset + 30,
                "HALF_OPEN -> CLOSED",
            )
            logs = compose("logs", "--no-color", "--tail=300", "booking-service")
            assert "CLOSED -> OPEN" in logs
            assert "OPEN -> HALF_OPEN" in logs
            assert "HALF_OPEN -> CLOSED" in logs
            print("Circuit recovered: OPEN -> HALF_OPEN -> CLOSED", flush=True)
        finally:
            for service in stopped:
                compose("start", service)


if __name__ == "__main__":
    main()
