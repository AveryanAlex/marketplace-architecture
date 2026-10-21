from uuid import uuid4

import pytest


async def test_crud_filters_pagination_and_soft_delete(api, product, fetch):
    p = await product(description=None, price=10.01)
    inactive = await product(name="Tea", category="drinks", status="INACTIVE")
    await product(name="Another coffee")
    response = await api("GET", f"/products/{p['id']}")
    assert response.status_code == 200 and response.json()["description"] is None
    page = (await api("GET", "/products?page=1&size=1")).json()
    assert page["totalElements"] == 3 and page["page"] == 1 and page["size"] == 1
    assert len(page["items"]) == 1
    filtered = (await api("GET", "/products?status=INACTIVE&category=drinks")).json()
    assert filtered["totalElements"] == 1 and filtered["items"][0]["id"] == inactive["id"]
    assert (await api("GET", "/products?category=Drink")).json()["totalElements"] == 0
    assert (await api("GET", "/products?page=100")).json()["items"] == []
    response = await api(
        "PUT",
        f"/products/{p['id']}",
        actor="seller",
        json=dict(name="Changed", price=11.11, stock=10, category="food", status="ACTIVE"),
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["price"] == 11.11
    assert updated["created_at"] == p["created_at"]
    assert updated["updated_at"] > p["updated_at"]
    for _ in range(2):
        response = await api("DELETE", f"/products/{p['id']}", actor="seller")
        assert response.status_code == 204 and response.content == b""
    row = (await fetch("SELECT status, price FROM products WHERE id = :id", id=p["id"]))[0]
    assert row["status"] == "ARCHIVED"
    assert (await api("GET", f"/products/{p['id']}")).json()["status"] == "ARCHIVED"
    assert (await api("GET", "/products?status=ARCHIVED")).json()["totalElements"] == 1


async def test_product_roles_and_seller_ownership(api, product, actors):
    p = await product()
    payload = dict(name="X", price=1, stock=1, category="x", status="ACTIVE")
    for method, path, kwargs in [
        ("POST", "/products", {"json": payload}),
        ("PUT", f"/products/{p['id']}", {"json": payload}),
        ("DELETE", f"/products/{p['id']}", {}),
    ]:
        response = await api(method, path, **kwargs)
        assert response.status_code == 403 and response.json()["error_code"] == "ACCESS_DENIED"
    for method in ("PUT", "DELETE"):
        response = await api(
            method,
            f"/products/{p['id']}",
            actor="seller2",
            **({"json": payload} if method == "PUT" else {}),
        )
        assert response.status_code == 403
    response = await api(
        "POST", "/products", actor="seller", json={**payload, "seller_id": actors["seller2"]["id"]}
    )
    assert response.status_code == 403
    response = await api(
        "POST", "/products", actor="admin", json={**payload, "seller_id": actors["seller2"]["id"]}
    )
    assert response.status_code == 201 and response.json()["seller_id"] == actors["seller2"]["id"]
    assert (
        await api("PUT", f"/products/{p['id']}", actor="admin", json=payload)
    ).status_code == 200
    assert (await api("DELETE", f"/products/{p['id']}", actor="admin")).status_code == 204
    for role in ("user", "seller", "admin"):
        assert (await api("GET", "/products", actor=role)).status_code == 200
        assert (await api("GET", f"/products/{p['id']}", actor=role)).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "x" * 256),
        ("description", "x" * 4001),
        ("price", 0),
        ("price", -1),
        ("price", 0.001),
        ("price", "1.00"),
        ("price", True),
        ("stock", -1),
        ("stock", 1.5),
        ("stock", "1"),
        ("stock", True),
        ("category", ""),
        ("category", "x" * 101),
        ("status", "UNKNOWN"),
        ("name", None),
        ("status", None),
        ("stock", None),
        ("extra", "forbidden"),
    ],
)
async def test_product_validation_before_business(api, field, value, fetch):
    payload = dict(name="X", price=1, stock=1, category="x", status="ACTIVE")
    payload[field] = value
    response = await api("POST", "/products", actor="seller", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert any(field in e["field"] for e in response.json()["details"]["fields"])
    assert not await fetch("SELECT id FROM products")


@pytest.mark.parametrize(
    "query",
    ["page=-1", "size=0", "size=101", "page=abc", "size=1.5", "status=UNKNOWN", "category="],
)
async def test_query_validation(api, query):
    response = await api("GET", "/products?" + query)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_product_missing_id_uuid_and_required_body(api):
    response = await api("GET", f"/products/{uuid4()}")
    assert response.status_code == 404 and response.json()["error_code"] == "PRODUCT_NOT_FOUND"
    response = await api("GET", "/products/not-a-uuid")
    assert response.status_code == 400
    response = await api("POST", "/products", actor="seller")
    assert response.status_code == 400
    response = await api(
        "POST",
        "/products",
        actor="seller",
        content='{"name":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_money_precision_and_boundaries(api, product):
    p = await product(
        name="x" * 255, description="x" * 4000, category="x" * 100, price=0.01, stock=0
    )
    assert p["price"] == 0.01
    p = await product(price=9999999999.99)
    assert p["price"] == 9999999999.99
    body = '{"name":"X","price":0.0100000000000000001,"stock":1,"category":"x","status":"ACTIVE"}'
    response = await api(
        "POST",
        "/products",
        actor="seller",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
