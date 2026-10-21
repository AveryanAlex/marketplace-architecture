from uuid import uuid4

import pytest

from app.core.config import settings


def items(product, quantity=1):
    return [{"product_id": product["id"], "quantity": quantity}]


async def test_create_snapshot_and_operation(api, product, order, fetch):
    p = await product(price=12.34, stock=10)
    o = await order(items=items(p, 3))
    assert o["status"] == "CREATED" and o["total_amount"] == 37.02
    assert o["discount_amount"] == 0
    assert o["items"][0]["price_at_order"] == 12.34
    row = (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]
    assert row["stock"] == 7
    assert (await fetch("SELECT operation_type FROM user_operations")) == [
        {"operation_type": "CREATE_ORDER"}
    ]
    response = await api(
        "PUT",
        "/products/" + p["id"],
        actor="seller",
        json=dict(name="Changed", price=99, stock=7, category="food", status="ACTIVE"),
    )
    assert response.status_code == 200
    response = await api("GET", "/orders/" + o["id"])
    assert response.json()["total_amount"] == 37.02
    assert response.json()["items"][0]["price_at_order"] == 12.34


async def test_catalog_errors_and_all_shortages_are_atomic(api, product, fetch):
    p = await product(stock=1)
    q = await product(stock=0)
    missing = {"id": str(uuid4())}
    response = await api("POST", "/orders", json={"items": items(missing)})
    assert response.status_code == 404 and response.json()["error_code"] == "PRODUCT_NOT_FOUND"
    for status in ("INACTIVE", "ARCHIVED"):
        inactive = await product(status=status)
        response = await api("POST", "/orders", json={"items": items(inactive)})
        assert response.status_code == 409 and response.json()["error_code"] == "PRODUCT_INACTIVE"
    response = await api("POST", "/orders", json={"items": items(p, 2) + items(q)})
    assert response.status_code == 409 and response.json()["error_code"] == "INSUFFICIENT_STOCK"
    shortages = response.json()["details"]["products"]
    assert len(shortages) == 2
    assert {x["product_id"] for x in shortages} == {p["id"], q["id"]}
    assert not await fetch("SELECT id FROM orders")
    assert not await fetch("SELECT id FROM user_operations")
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 1


async def test_update_stock_price_and_full_rollback(api, product, order, fetch):
    p = await product(stock=10, price=100)
    q = await product(stock=2, price=50)
    o = await order(items=items(p, 3))
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 1) + items(q, 3)})
    assert response.status_code == 409 and response.json()["error_code"] == "INSUFFICIENT_STOCK"
    rows = await fetch("SELECT id,stock FROM products ORDER BY id")
    assert {str(x["id"]): x["stock"] for x in rows} == {p["id"]: 7, q["id"]: 2}
    unchanged = (await api("GET", "/orders/" + o["id"])).json()
    assert unchanged == o
    assert len(await fetch("SELECT id FROM user_operations")) == 1
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 8) + items(q, 1)})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["total_amount"] == 850
    assert updated["updated_at"] > o["updated_at"]
    assert len(updated["items"]) == 2
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 2
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=q["id"]))[0]["stock"] == 1
    assert len(await fetch("SELECT id FROM user_operations")) == 2


async def test_update_missing_or_inactive_rolls_back_old_stock(api, product, order, fetch):
    p = await product(stock=5)
    q = await product(status="INACTIVE")
    o = await order(items=items(p, 2))
    for data, code in [
        (items({"id": str(uuid4())}), "PRODUCT_NOT_FOUND"),
        (items(q), "PRODUCT_INACTIVE"),
    ]:
        response = await api("PUT", "/orders/" + o["id"], json={"items": data})
        assert response.json()["error_code"] == code
        assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 3
        assert (await api("GET", "/orders/" + o["id"])).json() == o


async def test_cancel_restores_archived_products_once(api, product, order, fetch):
    p = await product(stock=5)
    o = await order(items=items(p, 2))
    assert (await api("DELETE", "/products/" + p["id"], actor="seller")).status_code == 204
    response = await api("POST", "/orders/" + o["id"] + "/cancel")
    assert response.status_code == 200 and response.json()["status"] == "CANCELED"
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 5
    response = await api("POST", "/orders/" + o["id"] + "/cancel")
    assert (
        response.status_code == 409 and response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    )
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 5


async def test_owner_role_and_admin_override(api, order, product):
    p = await product()
    o = await order(items=items(p))
    path = "/orders/" + o["id"]
    operations = [
        ("GET", path, {}),
        ("PUT", path, {"json": {"items": items(p)}}),
        ("POST", path + "/cancel", {}),
        ("POST", path + "/status", {"json": {"status": "PAYMENT_PENDING"}}),
    ]
    for method, url, kwargs in operations:
        response = await api(method, url, actor="user2", **kwargs)
        assert (
            response.status_code == 403
            and response.json()["error_code"] == "ORDER_OWNERSHIP_VIOLATION"
        )
        response = await api(method, url, actor="seller", **kwargs)
        assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"
    response = await api("POST", "/orders", actor="seller", json={"items": items(p)})
    assert response.status_code == 403
    assert (await api("GET", path, actor="admin")).status_code == 200
    assert (await api("PUT", path, actor="admin", json={"items": items(p, 2)})).status_code == 200
    assert (await api("POST", path + "/cancel", actor="admin")).status_code == 200
    admin_order = await order(actor="admin", items=items(p))
    assert admin_order["user_id"] != o["user_id"]


async def test_missing_order_endpoints(api, product):
    p = await product()
    path = "/orders/" + str(uuid4())
    for method, url, kwargs in [
        ("GET", path, {}),
        ("PUT", path, {"json": {"items": items(p)}}),
        ("POST", path + "/cancel", {}),
        ("POST", path + "/status", {"json": {"status": "PAID"}}),
    ]:
        response = await api(method, url, **kwargs)
        assert response.status_code == 404 and response.json()["error_code"] == "ORDER_NOT_FOUND"


async def test_full_state_machine(api, order, product, fetch):
    p = await product(stock=10)
    o = await order(items=items(p, 2))
    path = "/orders/" + o["id"]
    response = await api("POST", path + "/status", actor="admin", json={"status": "PAID"})
    assert (
        response.status_code == 409 and response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    )
    response = await api("POST", path + "/status", json={"status": "PAYMENT_PENDING"})
    assert response.status_code == 200
    response = await api("PUT", path, json={"items": items(p)})
    assert (
        response.status_code == 409 and response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    )
    response = await api("POST", path + "/status", json={"status": "PAID"})
    assert response.status_code == 403
    response = await api("POST", path + "/status", actor="admin", json={"status": "PAID"})
    assert response.status_code == 200
    response = await api("POST", path + "/cancel")
    assert response.status_code == 409
    # PAID is no longer an active order according to the assignment.
    await order(items=items(p))
    for target in ("SHIPPED", "COMPLETED"):
        assert (
            await api("POST", path + "/status", actor="admin", json={"status": target})
        ).status_code == 200
    for target in ("PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"):
        response = await api("POST", path + "/status", actor="admin", json={"status": target})
        assert response.status_code == 409
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 7


async def test_cancel_payment_pending(api, order, product):
    p = await product()
    o = await order(items=items(p))
    path = "/orders/" + o["id"]
    assert (
        await api("POST", path + "/status", json={"status": "PAYMENT_PENDING"})
    ).status_code == 200
    assert (await api("POST", path + "/cancel")).status_code == 200
    assert (
        await api("POST", path + "/status", actor="admin", json={"status": "PAYMENT_PENDING"})
    ).status_code == 409


async def test_active_order_and_cooldown_check_order(api, order, product, fetch):
    p = await product()
    o = await order(items=items(p))
    response = await api("POST", "/orders", json={"items": items(p)})
    assert response.status_code == 409 and response.json()["error_code"] == "ORDER_HAS_ACTIVE"
    settings.order_cooldown_minutes = 1
    # Cooldown precedes both active-order and catalogue checks.
    response = await api("POST", "/orders", json={"items": items({"id": str(uuid4())})})
    assert response.status_code == 429 and response.json()["error_code"] == "ORDER_LIMIT_EXCEEDED"
    assert int(response.headers["Retry-After"]) > 0
    # CREATE and UPDATE cooldowns are independent; first update succeeds.
    assert (await api("PUT", "/orders/" + o["id"], json={"items": items(p, 2)})).status_code == 200
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 3)})
    assert response.status_code == 429 and response.json()["error_code"] == "ORDER_LIMIT_EXCEEDED"
    # Owner and state checks precede the update cooldown.
    response = await api("PUT", "/orders/" + o["id"], actor="user2", json={"items": items(p)})
    assert (
        response.status_code == 403 and response.json()["error_code"] == "ORDER_OWNERSHIP_VIOLATION"
    )
    await api("POST", "/orders/" + o["id"] + "/status", json={"status": "PAYMENT_PENDING"})
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p)})
    assert (
        response.status_code == 409 and response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    )
    assert len(await fetch("SELECT id FROM user_operations")) == 2


async def test_cooldown_expiry(api, product, order):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.core.database import SessionFactory
    from app.orders.models import UserOperation

    p = await product()
    o = await order(items=items(p))
    await api("POST", "/orders/" + o["id"] + "/cancel")
    settings.order_cooldown_minutes = 1
    async with SessionFactory() as session, session.begin():
        await session.execute(
            update(UserOperation).values(created_at=datetime.now(UTC) - timedelta(minutes=2))
        )
    assert (await api("POST", "/orders", json={"items": items(p)})).status_code == 201


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"items": []}, "items"),
        ({"items": [{"product_id": str(uuid4()), "quantity": 1}] * 51}, "items"),
        ({"items": [{"product_id": str(uuid4()), "quantity": 0}]}, "quantity"),
        ({"items": [{"product_id": str(uuid4()), "quantity": 1000}]}, "quantity"),
        ({"items": [{"product_id": str(uuid4()), "quantity": True}]}, "quantity"),
        ({"items": [{"product_id": str(uuid4()), "quantity": 1.5}]}, "quantity"),
        ({"items": [{"product_id": "invalid", "quantity": 1}]}, "product_id"),
        (
            {"items": [{"product_id": str(uuid4()), "quantity": 1}], "promo_code": "bad-code"},
            "promo_code",
        ),
        (
            {"items": [{"product_id": str(uuid4()), "quantity": 1}], "promo_code": "ABC"},
            "promo_code",
        ),
        (
            {"items": [{"product_id": str(uuid4()), "quantity": 1}], "promo_code": "A" * 21},
            "promo_code",
        ),
    ],
)
async def test_order_validation(api, payload, field, fetch):
    response = await api("POST", "/orders", json=payload)
    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert any(field in error["field"] for error in response.json()["details"]["fields"])
    assert not await fetch("SELECT id FROM orders")
    assert not await fetch("SELECT id FROM user_operations")


async def test_duplicate_items_rejected_before_stock_checks(api, product, fetch):
    p = await product(stock=5)
    response = await api("POST", "/orders", json={"items": items(p, 3) + items(p, 3)})
    assert response.status_code == 400
    assert response.json()["details"]["fields"][0]["field"] == "body.items"
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 5


async def test_order_amount_overflow_rolls_back(api, product, fetch):
    p = await product(price=9999999999.99, stock=2)
    response = await api("POST", "/orders", json={"items": items(p, 2)})
    assert response.status_code == 400 and response.json()["error_code"] == "VALIDATION_ERROR"
    assert not await fetch("SELECT id FROM orders")
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 2


async def test_unknown_state_is_validation_error(api, order):
    o = await order()
    response = await api(
        "POST", "/orders/" + o["id"] + "/status", actor="admin", json={"status": "UNKNOWN"}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_stock_update_leaves_room_for_cancel(api, product, order, fetch):
    p = await product(stock=5)
    o = await order(items=items(p, 2))
    payload = dict(name="X", price=1, stock=2147483647, category="x", status="ACTIVE")
    response = await api("PUT", "/products/" + p["id"], actor="seller", json=payload)
    assert response.status_code == 400
    assert response.json()["details"]["fields"][0]["field"] == "body.stock"
    assert (await api("POST", "/orders/" + o["id"] + "/cancel")).status_code == 200
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 5


async def test_maximum_item_count_and_quantity(api, product, order):
    products = [await product(stock=999) for _ in range(50)]
    positions = items(products[0], 999) + [i for p in products[1:] for i in items(p)]
    created = await order(items=positions)
    assert len(created["items"]) == 50
    assert sum(i["quantity"] for i in created["items"]) == 1048
    assert (await api("POST", "/orders/" + created["id"] + "/cancel")).status_code == 200
