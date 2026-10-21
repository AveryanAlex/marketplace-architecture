import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from marketplace_api.models.order_response import OrderResponse
from marketplace_api.models.order_status import OrderStatus
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.auth.models import User
from app.auth.security import require_role
from app.core.config import settings
from app.core.errors import APIError, invalid_field
from app.orders.models import Order, OrderItem, UserOperation
from app.products.models import Product
from app.promo_codes.models import PromoCode
from app.promo_codes.service import applicable, discount

MAX_AMOUNT = Decimal("9999999999.99")
ACTIVE = (OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING)
NEXT_STATE = {
    OrderStatus.CREATED: OrderStatus.PAYMENT_PENDING,
    OrderStatus.PAYMENT_PENDING: OrderStatus.PAID,
    OrderStatus.PAID: OrderStatus.SHIPPED,
    OrderStatus.SHIPPED: OrderStatus.COMPLETED,
}


def check_owner(order, actor):
    if actor.role != "ADMIN" and order.user_id != actor.id:
        raise APIError(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")


async def lock_users(session, *ids):
    # An existing users row is also the lock for a user with no operations/orders yet.
    await session.scalars(
        select(User).where(User.id.in_(set(ids))).order_by(User.id).with_for_update()
    )


async def check_cooldown(session, actor, operation_type):
    last = await session.scalar(
        select(func.max(UserOperation.created_at)).where(
            UserOperation.user_id == actor.id,
            UserOperation.operation_type == operation_type,
        )
    )
    now = datetime.now(UTC)
    if last and now < last + timedelta(minutes=settings.order_cooldown_minutes):
        seconds = max(
            1,
            math.ceil(
                (last + timedelta(minutes=settings.order_cooldown_minutes) - now).total_seconds()
            ),
        )
        raise APIError(
            429,
            "ORDER_LIMIT_EXCEEDED",
            "Order operation cooldown has not elapsed",
            {"operation_type": operation_type, "retry_after_seconds": seconds},
            {"Retry-After": str(seconds)},
        )


def record_operation(session, actor, operation_type):
    session.add(
        UserOperation(user_id=actor.id, operation_type=operation_type, created_at=datetime.now(UTC))
    )


async def lock_products(session, ids):
    rows = await session.scalars(
        select(Product).where(Product.id.in_(set(ids))).order_by(Product.id).with_for_update()
    )
    return {p.id: p for p in rows}


def check_catalog(items, products):
    # Check the entire catalogue before checking any stock, as required by the contract.
    for item in items:
        product = products.get(item.product_id)
        if product is None:
            raise APIError(
                404, "PRODUCT_NOT_FOUND", "Product not found", {"product_id": str(item.product_id)}
            )
        if product.status != "ACTIVE":
            raise APIError(
                409,
                "PRODUCT_INACTIVE",
                "Only ACTIVE products can be ordered",
                {"product_id": str(item.product_id)},
            )


def reserve(items, products):
    shortages = [
        dict(
            product_id=str(i.product_id),
            requested_quantity=i.quantity,
            available_stock=products[i.product_id].stock,
        )
        for i in items
        if products[i.product_id].stock < i.quantity
    ]
    if shortages:
        raise APIError(409, "INSUFFICIENT_STOCK", "Insufficient stock", {"products": shortages})
    for item in items:
        products[item.product_id].stock -= item.quantity


def new_items(items, products):
    return [
        OrderItem(
            product_id=i.product_id,
            quantity=i.quantity,
            price_at_order=products[i.product_id].price,
        )
        for i in items
    ]


def subtotal(items):
    amount = sum((i.price_at_order * i.quantity for i in items), start=Decimal("0.00"))
    if amount > MAX_AMOUNT:
        invalid_field("body.items", "Order amount exceeds NUMERIC(12,2) capacity")
    return amount


async def apply_new_promo(session, code, amount):
    if code is None:
        return None, Decimal("0.00")
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == code).with_for_update())
    now = datetime.now(UTC)
    if (
        promo is None
        or not promo.active
        or promo.current_uses >= promo.max_uses
        or not promo.valid_from <= now <= promo.valid_until
    ):
        raise APIError(422, "PROMO_CODE_INVALID", "Promo code is invalid, expired or exhausted")
    if amount < promo.min_order_amount:
        raise APIError(
            422,
            "PROMO_CODE_MIN_AMOUNT",
            "Order amount is below promo minimum",
            {"min_order_amount": str(promo.min_order_amount), "order_amount": str(amount)},
        )
    promo.current_uses += 1
    return promo.id, discount(promo, amount)


async def create(session, actor, data):
    require_role(actor, "USER", "ADMIN")
    async with session.begin():
        await lock_users(session, actor.id)
        await check_cooldown(session, actor, "CREATE_ORDER")
        active = await session.scalar(
            select(Order.id)
            .where(
                Order.user_id == actor.id,
                Order.status.in_(ACTIVE),
            )
            .limit(1)
        )
        if active:
            raise APIError(409, "ORDER_HAS_ACTIVE", "User already has an active order")
        products = await lock_products(session, [i.product_id for i in data.items])
        check_catalog(data.items, products)
        reserve(data.items, products)
        items = new_items(data.items, products)
        amount = subtotal(items)
        promo_id, reduction = await apply_new_promo(session, data.promo_code, amount)
        order = Order(
            user_id=actor.id,
            status=OrderStatus.CREATED,
            items=items,
            promo_code_id=promo_id,
            discount_amount=reduction,
            total_amount=amount - reduction,
        )
        session.add(order)
        record_operation(session, actor, "CREATE_ORDER")
        await session.flush()
        await session.refresh(order, ["created_at", "updated_at"])
        return OrderResponse.model_validate(order)


async def get(session, actor, order_id):
    require_role(actor, "USER", "ADMIN")
    async with session.begin():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        order = await session.scalar(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        if order is None:
            raise APIError(404, "ORDER_NOT_FOUND", "Order not found")
        check_owner(order, actor)
        return OrderResponse.model_validate(order)


async def locked_order(session, actor, order_id):
    require_role(actor, "USER", "ADMIN")
    owner_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
    if owner_id is None:
        raise APIError(404, "ORDER_NOT_FOUND", "Order not found")
    if actor.role != "ADMIN" and owner_id != actor.id:
        raise APIError(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    # Same order everywhere: users (sorted), order, products (sorted), promo.
    await lock_users(session, actor.id, owner_id)
    return await session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items))
        .with_for_update()
    )


async def update(session, actor, order_id, data):
    require_role(actor, "USER", "ADMIN")
    async with session.begin():
        order = await locked_order(session, actor, order_id)
        if order.status != OrderStatus.CREATED:
            raise APIError(409, "INVALID_STATE_TRANSITION", "Only CREATED orders can be updated")
        await check_cooldown(session, actor, "UPDATE_ORDER")
        ids = [i.product_id for i in order.items] + [i.product_id for i in data.items]
        products = await lock_products(session, ids)
        for item in order.items:
            products[item.product_id].stock += item.quantity
        check_catalog(data.items, products)
        reserve(data.items, products)
        items = new_items(data.items, products)
        amount = subtotal(items)
        reduction = Decimal("0.00")
        if order.promo_code_id:
            promo = await session.scalar(
                select(PromoCode)
                .where(
                    PromoCode.id == order.promo_code_id,
                )
                .with_for_update()
            )
            # The current order already occupies one usage slot; it must not increment it again.
            if applicable(promo, amount, own_use=True):
                reduction = discount(promo, amount)
            else:
                promo.current_uses -= 1
                order.promo_code_id = None
        # Delete old rows before inserting replacements with the same (order_id, product_id).
        order.items.clear()
        await session.flush()
        order.items.extend(items)
        order.discount_amount, order.total_amount = reduction, amount - reduction
        # A no-op items replacement still counts as a successful update.
        order.updated_at = datetime.now(UTC)
        record_operation(session, actor, "UPDATE_ORDER")
        await session.flush()
        await session.refresh(order, ["updated_at"])
        return OrderResponse.model_validate(order)


async def cancel(session, actor, order_id):
    require_role(actor, "USER", "ADMIN")
    async with session.begin():
        order = await locked_order(session, actor, order_id)
        if order.status not in ACTIVE:
            raise APIError(
                409,
                "INVALID_STATE_TRANSITION",
                "Only CREATED or PAYMENT_PENDING orders can be canceled",
            )
        products = await lock_products(session, [i.product_id for i in order.items])
        for item in order.items:
            products[item.product_id].stock += item.quantity
        if order.promo_code_id:
            promo = await session.scalar(
                select(PromoCode)
                .where(
                    PromoCode.id == order.promo_code_id,
                )
                .with_for_update()
            )
            promo.current_uses -= 1
        order.status = OrderStatus.CANCELED
        await session.flush()
        await session.refresh(order, ["updated_at"])
        return OrderResponse.model_validate(order)


async def change_status(session, actor, order_id, data):
    require_role(actor, "USER", "ADMIN")
    async with session.begin():
        order = await locked_order(session, actor, order_id)
        target = OrderStatus(data.status)
        if actor.role != "ADMIN" and target != OrderStatus.PAYMENT_PENDING:
            raise APIError(403, "ACCESS_DENIED", "Only ADMIN can confirm payment or fulfillment")
        if NEXT_STATE.get(order.status) != target:
            raise APIError(
                409,
                "INVALID_STATE_TRANSITION",
                "Invalid order status transition",
                {"from": order.status.value, "to": target.value},
            )
        order.status = target
        await session.flush()
        await session.refresh(order, ["updated_at"])
        return OrderResponse.model_validate(order)
