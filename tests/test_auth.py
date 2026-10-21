import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings


async def test_registration_login_refresh(client, fetch):
    payload = {"username": "new_user", "password": "Password-123"}
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "USER"
    row = (await fetch("SELECT password_hash FROM users WHERE username = :name", name="new_user"))[
        0
    ]
    assert row["password_hash"].startswith("$argon2")
    assert row["password_hash"] != payload["password"]
    response = await client.post("/auth/login", json=payload)
    assert response.status_code == 200, response.text
    tokens = response.json()
    for kind in ("access", "refresh"):
        claims = jwt.decode(tokens[f"{kind}_token"], options={"verify_signature": False})
        assert claims["role"] == "USER" and claims["type"] == kind
        duration = claims["exp"] - claims["iat"]
        assert duration == (20 * 60 if kind == "access" else 14 * 86400)
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    assert refreshed.json()["access_token"] != tokens["access_token"]
    reused = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    assert reused.json()["error_code"] == "REFRESH_TOKEN_INVALID"
    response = await client.get(
        "/products", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    )
    assert response.status_code == 200


async def test_duplicate_registration_and_invalid_login(client):
    payload = dict(username="duplicate", password="Password-123", role="SELLER")
    assert (await client.post("/auth/register", json=payload)).status_code == 201
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 409
    assert response.json()["error_code"] == "USER_ALREADY_EXISTS"
    for username in ("duplicate", "unknown"):
        response = await client.post(
            "/auth/login", json=dict(username=username, password="Wrong-password")
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "TOKEN_INVALID"


async def test_cannot_register_admin(client, fetch):
    response = await client.post(
        "/auth/register", json=dict(username="evil", password="Password-123", role="ADMIN")
    )
    assert response.status_code == 400
    assert not await fetch("SELECT id FROM users")


@pytest.mark.parametrize("token", [None, "broken", "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0."])
async def test_invalid_access(client, token):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    response = await client.get("/products", headers=headers)
    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_INVALID"
    assert "X-Request-Id" in response.headers


async def test_expired_wrong_type_and_wrong_audience(api, actors, client):
    token = actors["user"]["tokens"]["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    response = await api("GET", "/products", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_EXPIRED"
    response = await api(
        "GET",
        "/products",
        headers={"Authorization": f"Bearer {actors['user']['tokens']['refresh_token']}"},
    )
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"
    response = await client.post("/auth/refresh", json={"refresh_token": token})
    assert response.status_code == 401 and response.json()["error_code"] == "REFRESH_TOKEN_INVALID"
    claims["exp"] = datetime.now(UTC) + timedelta(minutes=20)
    claims["aud"] = "another-service"
    wrong_audience = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    response = await api("GET", "/products", headers={"Authorization": f"Bearer {wrong_audience}"})
    assert response.status_code == 401 and response.json()["error_code"] == "TOKEN_INVALID"


async def test_refresh_rotation_is_atomic(client, actors):
    token = actors["user"]["tokens"]["refresh_token"]
    results = await asyncio.gather(
        *(client.post("/auth/refresh", json={"refresh_token": token}) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [200, 401]
    assert (
        next(r for r in results if r.status_code == 401).json()["error_code"]
        == "REFRESH_TOKEN_INVALID"
    )
