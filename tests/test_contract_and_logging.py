import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from openapi_spec_validator import validate
from sqlalchemy import text

from app import handlers
from app.core.config import settings
from app.core.database import SessionFactory


def test_authored_spec_valid_and_generated_routes_are_used():
    from marketplace_api.apis import auth_api, health_api, orders_api, products_api, promo_codes_api

    spec = yaml.safe_load(Path("openapi/marketplace.yaml").read_text())
    validate(spec)
    actual = {}
    for module in (auth_api, health_api, orders_api, products_api, promo_codes_api):
        assert "/generated/" in str(Path(module.__file__).resolve())
        for route in module.router.routes:
            for method in route.methods:
                actual[(route.path, method.lower())] = route
    expected = {(path, method) for path, methods in spec["paths"].items() for method in methods}
    assert set(actual) == expected
    for path, method in expected:
        operation = spec["paths"][path][method]
        route = actual[(path, method)]
        assert route.endpoint.__name__ == operation["operationId"]
        assert route.status_code == operation["x-success-status"]


async def test_invalid_request_never_reaches_handler(api, monkeypatch):
    async def forbidden(**kwargs):
        pytest.fail("Invalid request reached application implementation")

    monkeypatch.setattr(handlers, "create_product", forbidden)
    response = await api(
        "POST",
        "/products",
        actor="seller",
        json=dict(name="", price=0, stock=-1, category="", status="WRONG"),
    )
    assert response.status_code == 400
    fields = response.json()["details"]["fields"]
    assert {f["field"] for f in fields} >= {
        "body.name",
        "body.price",
        "body.stock",
        "body.category",
        "body.status",
    }


async def test_request_logs_json_fields_and_masking(client, capsys):
    payload = dict(username="log_user", password="Secret-password-123")
    registered = await client.post("/auth/register", json=payload)
    assert registered.status_code == 201
    logged_in = await client.post("/auth/login", json=payload)
    assert logged_in.status_code == 200
    tokens = logged_in.json()
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    invalid = await client.get("/products", headers={"Authorization": "Bearer invalid"})
    assert invalid.status_code == 401
    output = capsys.readouterr().out
    logs = [json.loads(line) for line in output.splitlines() if line.strip()]
    requests = [event for event in logs if event["event"] == "api_request"]
    assert len(requests) == 4
    for event in requests:
        assert {
            "request_id",
            "method",
            "endpoint",
            "status_code",
            "duration_ms",
            "user_id",
            "timestamp",
        } <= event.keys()
        UUID(event["request_id"])
        assert event["duration_ms"] >= 0
    assert requests[0]["request_body"]["password"] == "***"
    assert requests[1]["request_body"]["password"] == "***"
    assert requests[2]["request_body"]["refresh_token"] == "***"
    assert requests[0]["request_id"] == registered.headers["X-Request-Id"]
    assert requests[1]["user_id"] == registered.json()["id"]
    assert requests[-1]["user_id"] is None
    assert payload["password"] not in output
    assert tokens["access_token"] not in output
    assert tokens["refresh_token"] not in output


async def test_log_body_is_replayed_nested_secrets_masked_and_limited(client, capsys, monkeypatch):
    monkeypatch.setattr(settings, "log_body_limit", 100)
    payload = {
        "username": "abc",
        "password": "Password-123",
        "extra": {"PASSWORD": "nested-secret"},
    }
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 400
    out = capsys.readouterr().out
    assert "nested-secret" not in out and "Password-123" not in out
    assert "***" in out
    response = await client.post("/auth/register", json={**payload, "extra": "x" * 500})
    assert response.status_code == 400
    out = capsys.readouterr().out
    assert "<body omitted: log limit exceeded>" in out
    assert "Password-123" not in out
    response = await client.post(
        "/auth/login",
        content='{"password":"broken-secret"',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    out = capsys.readouterr().out
    assert "broken-secret" not in out and "<invalid JSON>" in out


async def test_request_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 1024)
    response = await client.post("/auth/login", content="x" * 2048)
    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    UUID(response.headers["X-Request-Id"])


async def test_internal_error_still_logs_and_has_request_id(client, monkeypatch, capsys):
    async def fail(**kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(handlers, "health", fail)
    response = await client.get("/health")
    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    event = next(e for e in events if e["event"] == "api_request")
    assert event["status_code"] == 500 and event["request_id"] == response.headers["X-Request-Id"]


async def test_db_timestamps_trigger_and_status_index(product, fetch):
    p = await product()
    async with SessionFactory() as session, session.begin():
        await session.execute(
            text("UPDATE products SET name='direct SQL' WHERE id=:id"), {"id": p["id"]}
        )
    row = (await fetch("SELECT created_at,updated_at FROM products WHERE id=:id", id=p["id"]))[0]
    assert row["updated_at"] > row["created_at"]
    index = await fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='products' "
        "AND indexname='ix_products_status'"
    )
    assert "(status)" in index[0]["indexdef"]


async def test_body_missing_and_invalid_utf8_are_validation_errors(client):
    for payload in (b"null", b"[]", b"{}", b"\xff"):
        response = await client.post(
            "/auth/register", content=payload, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400, response.text
        assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_nonfinite_numbers_are_rejected_and_logs_remain_json(client, capsys):
    response = await client.post(
        "/auth/register",
        content='{"username":"abc","password":NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    from app.core.contract import reject_constant

    events = [
        json.loads(line, parse_constant=reject_constant)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert events[-1]["request_body"] == "<invalid JSON>"
