"""Thin implementation adapters invoked by generated FastAPI routes."""

from fastapi import Response
from marketplace_api.models.health_response import HealthResponse

from app.auth import service as auth
from app.orders import service as orders
from app.products import service as products
from app.promo_codes import service as promos


async def health(*, request, session, actor):
    return HealthResponse(status="ok")


async def register(*, request, session, actor, register_request):
    return await auth.register(session, register_request)


async def login(*, request, session, actor, login_request):
    return await auth.login(session, login_request, request)


async def refresh(*, request, session, actor, refresh_request):
    return await auth.refresh(session, refresh_request, request)


async def create_product(*, request, session, actor, product_create):
    return await products.create(session, actor, product_create)


async def get_product(*, request, session, actor, id):
    return await products.get(session, id)


async def list_products(*, request, session, actor, page, size, status, category):
    return await products.list_products(session, page, size, status, category)


async def update_product(*, request, session, actor, id, product_update):
    return await products.update(session, actor, id, product_update)


async def archive_product(*, request, session, actor, id):
    await products.update(session, actor, id)
    return Response(status_code=204)


async def create_order(*, request, session, actor, order_create):
    return await orders.create(session, actor, order_create)


async def get_order(*, request, session, actor, id):
    return await orders.get(session, actor, id)


async def update_order(*, request, session, actor, id, order_update):
    return await orders.update(session, actor, id, order_update)


async def cancel_order(*, request, session, actor, id):
    return await orders.cancel(session, actor, id)


async def change_order_status(*, request, session, actor, id, order_status_update):
    return await orders.change_status(session, actor, id, order_status_update)


async def create_promo_code(*, request, session, actor, promo_code_create):
    return await promos.create(session, actor, promo_code_create)
