import asyncio

import pytest

from app.core.config import settings
from tests.test_orders import items


async def race(*requests):
    return await asyncio.wait_for(asyncio.gather(*requests), timeout=15)


async def test_last_unit_cannot_be_sold_twice(api, product, fetch):
    p = await product(stock=1)
    responses = await race(
        *(
            api("POST", "/orders", actor=actor, json={"items": items(p)})
            for actor in ("user", "user2")
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert (
        next(r for r in responses if r.status_code == 409).json()["error_code"]
        == "INSUFFICIENT_STOCK"
    )
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 0
    assert len(await fetch("SELECT id FROM orders")) == 1


@pytest.mark.parametrize(
    "cooldown,expected", [(0, "ORDER_HAS_ACTIVE"), (1, "ORDER_LIMIT_EXCEEDED")]
)
async def test_parallel_creates_for_same_user(api, product, fetch, cooldown, expected):
    settings.order_cooldown_minutes = cooldown
    p = await product(stock=10)
    responses = await race(*(api("POST", "/orders", json={"items": items(p)}) for _ in range(2)))
    assert sum(r.status_code == 201 for r in responses) == 1
    assert next(r for r in responses if r.status_code != 201).json()["error_code"] == expected
    assert len(await fetch("SELECT id FROM orders")) == 1
    assert len(await fetch("SELECT id FROM user_operations")) == 1
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 9


async def test_last_promo_slot_is_atomic(api, product, promo, fetch):
    p = await product(stock=5)
    q = await product(stock=5)
    c = await promo(max_uses=1)
    responses = await race(
        *(
            api(
                "POST", "/orders", actor=actor, json={"items": items(prod), "promo_code": c["code"]}
            )
            for actor, prod in [("user", p), ("user2", q)]
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 422]
    assert (
        next(r for r in responses if r.status_code == 422).json()["error_code"]
        == "PROMO_CODE_INVALID"
    )
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 1
    stocks = [row["stock"] for row in await fetch("SELECT stock FROM products")]
    assert sorted(stocks) == [4, 5]
    assert len(await fetch("SELECT id FROM orders")) == 1
    assert len(await fetch("SELECT id FROM user_operations")) == 1


async def test_double_cancel_restores_once(api, product, promo, order, fetch):
    p = await product(stock=10)
    c = await promo()
    o = await order(items=items(p, 2), promo_code=c["code"])
    responses = await race(*(api("POST", "/orders/" + o["id"] + "/cancel") for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 10
    assert (await fetch("SELECT current_uses FROM promo_codes WHERE id=:id", id=c["id"]))[0][
        "current_uses"
    ] == 0


async def test_update_cancel_race_keeps_inventory(api, product, order, fetch):
    p = await product(stock=10)
    o = await order(items=items(p, 2))
    updated, canceled = await race(
        api("PUT", "/orders/" + o["id"], json={"items": items(p, 4)}),
        api("POST", "/orders/" + o["id"] + "/cancel"),
    )
    assert canceled.status_code == 200
    assert updated.status_code in {200, 409}
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 10
    assert (await api("GET", "/orders/" + o["id"])).json()["status"] == "CANCELED"


async def test_parallel_updates_respect_cooldown(api, product, order, fetch):
    p = await product(stock=10)
    o = await order(items=items(p, 2))
    settings.order_cooldown_minutes = 1
    responses = await race(
        *(api("PUT", "/orders/" + o["id"], json={"items": items(p, 3)}) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 429]
    assert (await fetch("SELECT stock FROM products WHERE id=:id", id=p["id"]))[0]["stock"] == 7
    assert (
        len(await fetch("SELECT id FROM user_operations WHERE operation_type='UPDATE_ORDER'")) == 1
    )


async def test_reversed_item_order_does_not_deadlock(api, product, fetch):
    p = await product(stock=10)
    q = await product(stock=10)
    responses = await race(
        api("POST", "/orders", actor="user", json={"items": items(p) + items(q)}),
        api("POST", "/orders", actor="user2", json={"items": items(q) + items(p)}),
    )
    assert [r.status_code for r in responses] == [201, 201]
    assert [r["stock"] for r in await fetch("SELECT stock FROM products")] == [8, 8]
