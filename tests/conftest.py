import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://marketplace:marketplace@localhost:15433/marketplace_test"
)
os.environ.setdefault("ORDER_COOLDOWN_MINUTES", "0")

from marketplace_api.models.role import Role

from app.auth.models import User
from app.auth.security import issue_tokens, password_hasher
from app.core.config import settings
from app.core.database import SessionFactory, engine
from app.main import app


@pytest.fixture(autouse=True)
async def clean_database():
    if not make_url(settings.database_url).database.endswith("_test"):
        raise RuntimeError("Tests require a dedicated database ending in _test")
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, promo_codes CASCADE"))
    previous = settings.order_cooldown_minutes
    settings.order_cooldown_minutes = 0
    yield
    settings.order_cooldown_minutes = previous


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def actors():
    result = {}
    password_hash = password_hasher.hash("Password-123")
    async with SessionFactory() as session, session.begin():
        for name, role in [
            ("user", Role.USER),
            ("user2", Role.USER),
            ("seller", Role.SELLER),
            ("seller2", Role.SELLER),
            ("admin", Role.ADMIN),
        ]:
            user = User(username=name, password_hash=password_hash, role=role)
            session.add(user)
            await session.flush()
            tokens = issue_tokens(user, session)
            result[name] = dict(
                id=str(user.id),
                tokens=tokens.model_dump(),
                headers={"Authorization": f"Bearer {tokens.access_token}"},
            )
    return result


@pytest.fixture
async def api(client, actors):
    async def request(method, path, actor="user", **kwargs):
        headers = kwargs.pop("headers", {})
        if actor is not None:
            headers = {**actors[actor]["headers"], **headers}
        return await client.request(method, path, headers=headers, **kwargs)

    return request


@pytest.fixture
async def product(api):
    async def create(actor="seller", **overrides):
        payload = dict(name="Coffee", price=100, stock=20, category="food", status="ACTIVE")
        payload.update(overrides)
        response = await api("POST", "/products", actor=actor, json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
async def promo(api):
    from datetime import UTC, datetime, timedelta

    async def create(**overrides):
        now = datetime.now(UTC)
        payload = dict(
            code="SAVE10",
            discount_type="PERCENTAGE",
            discount_value=10,
            min_order_amount=100,
            max_uses=10,
            valid_from=(now - timedelta(days=1)).isoformat(),
            valid_until=(now + timedelta(days=1)).isoformat(),
            active=True,
        )
        payload.update(overrides)
        response = await api("POST", "/promo-codes", actor="seller", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
async def order(api, product):
    async def create(actor="user", items=None, **overrides):
        if items is None:
            p = await product()
            items = [{"product_id": p["id"], "quantity": 2}]
        response = await api("POST", "/orders", actor=actor, json={"items": items, **overrides})
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
async def fetch():
    async def query(sql, **parameters):
        async with SessionFactory() as session:
            result = await session.execute(text(sql), parameters)
            return [dict(row) for row in result.mappings()]

    return query


@pytest.fixture(scope="session", autouse=True)
async def shutdown_engine():
    yield
    await engine.dispose()
