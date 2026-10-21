from uuid import UUID


async def test_health_and_request_id(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    UUID(response.headers["X-Request-Id"])
    again = await client.get("/health", headers={"X-Request-Id": "not-trusted"})
    assert again.headers["X-Request-Id"] != response.headers["X-Request-Id"]
    UUID(again.headers["X-Request-Id"])


async def test_unknown_route_and_method_have_contract_errors(client):
    response = await client.get("/missing")
    assert response.status_code == 404
    assert response.json()["error_code"] == "ROUTE_NOT_FOUND"
    response = await client.post("/health")
    assert response.status_code == 405
    assert response.json()["error_code"] == "METHOD_NOT_ALLOWED"
