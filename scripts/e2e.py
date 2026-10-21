#!/usr/bin/env python3
"""Live HTTP + SQL defense demonstration. Uses only the Python standard library."""

import json
import os
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = os.getenv("API_URL", "").rstrip("/")
PASSWORD = "Demo-password-123"


def call(method, path, expected=200, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(BASE + path, method=method, headers=headers, data=data)
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as exc:
        response = exc
    body = response.read()
    result = json.loads(body) if body else None
    assert response.status == expected, (method, path, expected, response.status, result)
    assert response.headers.get("X-Request-Id"), (method, path, "Missing request ID")
    print(
        f"{method:6} {path} -> {response.status}",
        result.get("error_code", result.get("status", "")) if isinstance(result, dict) else "",
    )
    return result


def account(role):
    username = "demo_" + role.lower() + "_" + uuid4().hex[:10]
    user = call(
        "POST",
        "/auth/register",
        201,
        payload={
            "username": username,
            "password": PASSWORD,
            "role": role,
        },
    )
    tokens = call("POST", "/auth/login", payload={"username": username, "password": PASSWORD})
    return user, tokens


def sql(query):
    print("\nSQL:", query)
    output = subprocess.check_output(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "marketplace",
            "-d",
            "marketplace",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        ],
        text=True,
    ).strip()
    print(output)
    return json.loads(output)


def main():
    global BASE
    if not BASE:
        address = subprocess.check_output(
            ["docker", "compose", "port", "api", "8080"], text=True
        ).strip()
        BASE = "http://" + address
    config = json.loads(
        subprocess.check_output(["docker", "compose", "config", "--format", "json"], text=True)
    )
    environment = config["services"]["api"]["environment"]
    from datetime import UTC, datetime, timedelta

    call("GET", "/health")
    seller, seller_tokens = account("SELLER")
    buyer, buyer_tokens = account("USER")
    other, other_tokens = account("USER")
    admin_tokens = call(
        "POST",
        "/auth/login",
        payload={
            "username": environment["BOOTSTRAP_ADMIN_USERNAME"],
            "password": environment["BOOTSTRAP_ADMIN_PASSWORD"],
        },
    )
    seller_token = seller_tokens["access_token"]
    buyer_token = buyer_tokens["access_token"]
    admin_token = admin_tokens["access_token"]
    payload = dict(
        name="Demo coffee", description=None, price=100, stock=10, category="demo", status="ACTIVE"
    )
    p = call("POST", "/products", 201, seller_token, payload)
    call("POST", "/products", 403, buyer_token, payload)
    call("POST", "/products", 400, seller_token, {**payload, "price": 0})
    call("GET", "/products?page=0&size=1&status=ACTIVE&category=demo", token=buyer_token)
    call("GET", "/products", 401)
    now = datetime.now(UTC)
    promo = call(
        "POST",
        "/promo-codes",
        201,
        seller_token,
        dict(
            code="DEMO_" + uuid4().hex[:8].upper(),
            discount_type="PERCENTAGE",
            discount_value=10,
            min_order_amount=200,
            max_uses=5,
            active=True,
            valid_from=(now - timedelta(days=1)).isoformat(),
            valid_until=(now + timedelta(days=1)).isoformat(),
        ),
    )
    items = [{"product_id": p["id"], "quantity": 3}]
    call("POST", "/orders", 409, buyer_token, {"items": [{"product_id": p["id"], "quantity": 11}]})
    created = call(
        "POST", "/orders", 201, buyer_token, {"items": items, "promo_code": promo["code"]}
    )
    assert created["total_amount"] == 270 and created["discount_amount"] == 30
    evidence = sql(
        f"SELECT json_build_object('stock', p.stock, 'total', o.total_amount, "
        f"'discount', o.discount_amount, 'promo_uses', c.current_uses, "
        f"'snapshot', i.price_at_order) FROM products p, orders o, promo_codes c, "
        f"order_items i WHERE p.id='{p['id']}' AND o.id='{created['id']}' "
        f"AND c.id='{promo['id']}' AND i.order_id=o.id;"
    )
    assert evidence == {"stock": 7, "total": 270, "discount": 30, "promo_uses": 1, "snapshot": 100}
    order_path = "/orders/" + created["id"]
    call("GET", order_path, 403, other_tokens["access_token"])
    call(
        "PUT",
        "/products/" + p["id"],
        token=seller_token,
        payload={**payload, "price": 120, "stock": 7},
    )
    snapshot = call("GET", order_path, token=buyer_token)
    assert snapshot["items"][0]["price_at_order"] == 100 and snapshot["total_amount"] == 270
    updated = call(
        "PUT",
        order_path,
        token=buyer_token,
        payload={"items": [{"product_id": p["id"], "quantity": 1}]},
    )
    assert updated["total_amount"] == 120 and updated["promo_code_id"] is None
    evidence = sql(
        f"SELECT json_build_object('stock', p.stock, 'total', o.total_amount, "
        f"'discount', o.discount_amount, 'promo_id', o.promo_code_id, "
        f"'promo_uses', c.current_uses) FROM products p, orders o, promo_codes c "
        f"WHERE p.id='{p['id']}' AND o.id='{created['id']}' AND c.id='{promo['id']}';"
    )
    assert evidence == {"stock": 9, "total": 120, "discount": 0, "promo_id": None, "promo_uses": 0}
    call("POST", order_path + "/status", token=buyer_token, payload={"status": "PAYMENT_PENDING"})
    call("PUT", order_path, 409, buyer_token, {"items": items})
    call("POST", order_path + "/cancel", token=buyer_token)
    call("POST", order_path + "/cancel", 409, buyer_token)
    evidence = sql(
        f"SELECT json_build_object('stock', p.stock, 'status', o.status) "
        f"FROM products p, orders o WHERE p.id='{p['id']}' AND o.id='{created['id']}';"
    )
    assert evidence == {"stock": 10, "status": "CANCELED"}
    second = call("POST", "/orders", 201, other_tokens["access_token"], {"items": items})
    second_path = "/orders/" + second["id"]
    call("POST", second_path + "/status", 409, admin_token, {"status": "SHIPPED"})
    for status in ("PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"):
        call("POST", second_path + "/status", token=admin_token, payload={"status": status})
    call("POST", second_path + "/cancel", 409, admin_token)
    call("DELETE", "/products/" + p["id"], 204, seller_token)
    assert call("GET", "/products/" + p["id"], token=buyer_token)["status"] == "ARCHIVED"
    refreshed = call(
        "POST", "/auth/refresh", payload={"refresh_token": buyer_tokens["refresh_token"]}
    )
    call("POST", "/auth/refresh", 401, payload={"refresh_token": buyer_tokens["refresh_token"]})
    call("GET", "/products", token=refreshed["access_token"])
    evidence = sql(
        f"SELECT json_build_object('orders', (SELECT count(*) FROM orders WHERE user_id "
        f"IN ('{buyer['id']}', '{other['id']}')), 'operations', "
        f"(SELECT count(*) FROM user_operations WHERE user_id IN "
        f"('{buyer['id']}', '{other['id']}')), 'product_status', "
        f"(SELECT status FROM products WHERE id='{p['id']}'));"
    )
    assert evidence == {"orders": 2, "operations": 3, "product_status": "ARCHIVED"}
    print("\nE2E passed: HTTP requests and SELECT evidence verified.")


if __name__ == "__main__":
    main()
