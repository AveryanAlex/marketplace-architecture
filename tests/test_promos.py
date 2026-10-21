from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.core.database import SessionFactory
from app.promo_codes.models import PromoCode
from tests.test_orders import items


@pytest.mark.parametrize(
    "kind,value,expected_discount",
    [
        ("PERCENTAGE", 10, 20),
        ("PERCENTAGE", 70, 140),
        ("PERCENTAGE", 71, 0),
        ("PERCENTAGE", 100, 0),
        ("FIXED_AMOUNT", 50, 50),
        ("FIXED_AMOUNT", 500, 200),
    ],
)
async def test_discount_rules(api, product, promo, order, fetch, kind, value, expected_discount):
    p = await product(price=100, stock=10)
    c = await promo(discount_type=kind, discount_value=value)
    o = await order(items=items(p, 2), promo_code=c["code"])
    assert o["discount_amount"] == expected_discount
    assert o["total_amount"] == 200 - expected_discount
    assert o["promo_code_id"] == c["id"]
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 1
    canceled = await api("POST", "/orders/" + o["id"] + "/cancel")
    assert canceled.status_code == 200
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 10
    assert (await api("POST", "/orders/" + o["id"] + "/cancel")).status_code == 409
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0


async def test_discount_rounding(product, promo, order):
    p = await product(price=0.05)
    c = await promo(min_order_amount=0, discount_value=10)
    o = await order(items=items(p), promo_code=c["code"])
    assert o["discount_amount"] == 0.01 and o["total_amount"] == 0.04


@pytest.mark.parametrize(
    "case", ["missing", "inactive", "expired", "future", "exhausted", "minimum"]
)
async def test_invalid_promos_rollback_reservation(api, product, promo, fetch, case):
    p = await product(stock=10, price=100)
    now = datetime.now(UTC)
    overrides = {
        "inactive": {"active": False},
        "expired": {
            "valid_from": (now - timedelta(days=2)).isoformat(),
            "valid_until": (now - timedelta(days=1)).isoformat(),
        },
        "future": {
            "valid_from": (now + timedelta(days=1)).isoformat(),
            "valid_until": (now + timedelta(days=2)).isoformat(),
        },
        "minimum": {"min_order_amount": 500},
    }.get(case, {})
    if case == "missing":
        code = "NO_SUCH_CODE"
    else:
        c = await promo(**overrides)
        code = c["code"]
        if case == "exhausted":
            async with SessionFactory() as session, session.begin():
                await session.execute(update(PromoCode).values(current_uses=PromoCode.max_uses))
    response = await api("POST", "/orders", json={"items": items(p, 2), "promo_code": code})
    assert response.status_code == 422
    assert response.json()["error_code"] == (
        "PROMO_CODE_MIN_AMOUNT" if case == "minimum" else "PROMO_CODE_INVALID"
    )
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 10
    assert not await fetch("SELECT id FROM orders")
    assert not await fetch("SELECT id FROM user_operations")


async def test_update_retains_own_exhausted_slot_then_removes_below_minimum(
    api, product, promo, order, fetch
):
    p = await product(stock=10, price=100)
    c = await promo(max_uses=1, min_order_amount=200)
    o = await order(items=items(p, 2), promo_code=c["code"])
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 3)})
    assert response.status_code == 200, response.text
    assert response.json()["discount_amount"] == 30 and response.json()["promo_code_id"] == c["id"]
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 1
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 1)})
    assert response.status_code == 200
    assert response.json()["promo_code_id"] is None and response.json()["discount_amount"] == 0
    assert response.json()["total_amount"] == 100
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0
    assert (await api("POST", "/orders/" + o["id"] + "/cancel")).status_code == 200
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0


@pytest.mark.parametrize("change", ["expired", "inactive"])
async def test_update_removes_promo_that_stopped_applying(
    api, product, promo, order, fetch, change
):
    p = await product()
    c = await promo()
    o = await order(items=items(p, 2), promo_code=c["code"])
    async with SessionFactory() as session, session.begin():
        values = (
            {"active": False}
            if change == "inactive"
            else {"valid_until": datetime.now(UTC) - timedelta(seconds=1)}
        )
        await session.execute(update(PromoCode).values(**values))
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 3)})
    assert response.status_code == 200
    assert response.json()["promo_code_id"] is None
    assert response.json()["total_amount"] == 300
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0


async def test_failed_update_does_not_release_promo_usage(api, product, promo, order, fetch):
    p = await product(stock=5)
    c = await promo()
    o = await order(items=items(p, 2), promo_code=c["code"])
    response = await api("PUT", "/orders/" + o["id"], json={"items": items(p, 6)})
    assert response.status_code == 409
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 1
    assert (await api("GET", "/orders/" + o["id"])).json() == o


async def test_promo_roles_and_duplicate_code(api, promo):
    c = await promo()
    payload = {k: v for k, v in c.items() if k not in {"id", "current_uses"}}
    response = await api("POST", "/promo-codes", json=payload)
    assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"
    response = await api("POST", "/promo-codes", actor="seller", json=payload)
    assert response.status_code == 422 and response.json()["error_code"] == "PROMO_CODE_INVALID"
    response = await api(
        "POST", "/promo-codes", actor="admin", json={**payload, "code": "ADMIN_PROMO"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "bad"),
        ("discount_value", 0),
        ("discount_value", 101),
        ("max_uses", 0),
        ("min_order_amount", -1),
        ("active", None),
        ("valid_from", "2026-01-01T00:00:00"),
        ("valid_until", "2020-01-01T00:00:00Z"),
    ],
)
async def test_promo_validation(api, field, value, fetch):
    now = datetime.now(UTC)
    payload = dict(
        code="SAVE10",
        discount_type="PERCENTAGE",
        discount_value=10,
        min_order_amount=0,
        max_uses=1,
        valid_from=now.isoformat(),
        valid_until=(now + timedelta(days=1)).isoformat(),
    )
    payload[field] = value
    response = await api("POST", "/promo-codes", actor="seller", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert any(field in e["field"] for e in response.json()["details"]["fields"])
    assert not await fetch("SELECT id FROM promo_codes")
