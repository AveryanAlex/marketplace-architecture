from marketplace_api.models.product_page import ProductPage
from marketplace_api.models.product_response import ProductResponse
from marketplace_api.models.product_status import ProductStatus
from sqlalchemy import func, select

from app.auth.models import User
from app.auth.security import require_role
from app.core.errors import APIError, invalid_field
from app.orders.models import Order, OrderItem
from app.products.models import Product


def check_owner(product, actor):
    if actor.role != "ADMIN" and product.seller_id != actor.id:
        raise APIError(403, "ACCESS_DENIED", "Product belongs to another seller")


async def create(session, actor, data):
    require_role(actor, "SELLER", "ADMIN")
    seller_id = data.seller_id or actor.id
    if actor.role != "ADMIN" and seller_id != actor.id:
        raise APIError(403, "ACCESS_DENIED", "Cannot create a product for another seller")
    async with session.begin():
        seller = await session.get(User, seller_id)
        if seller is None or seller.role not in {"SELLER", "ADMIN"}:
            invalid_field("body.seller_id", "Seller must be an existing SELLER or ADMIN")
        product = Product(**data.model_dump(exclude={"seller_id"}), seller_id=seller_id)
        session.add(product)
        await session.flush()
        await session.refresh(product)
        return ProductResponse.model_validate(product)


async def get(session, product_id):
    async with session.begin():
        product = await session.get(Product, product_id)
        if product is None:
            raise APIError(404, "PRODUCT_NOT_FOUND", "Product not found")
        return ProductResponse.model_validate(product)


async def list_products(session, page, size, status, category):
    predicates = []
    if status is not None:
        predicates.append(Product.status == status)
    if category is not None:
        predicates.append(Product.category == category)
    async with session.begin():
        # Repeatable-read keeps COUNT and page data on the same database snapshot.
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        count = await session.scalar(select(func.count()).select_from(Product).where(*predicates))
        rows = await session.scalars(
            select(Product)
            .where(*predicates)
            .order_by(Product.created_at, Product.id)
            .offset(page * size)
            .limit(size)
        )
        return ProductPage(
            items=[ProductResponse.model_validate(p) for p in rows],
            totalElements=count,
            page=page,
            size=size,
        )


async def update(session, actor, product_id, data=None):
    require_role(actor, "SELLER", "ADMIN")
    async with session.begin():
        product = await session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        if product is None:
            raise APIError(404, "PRODUCT_NOT_FOUND", "Product not found")
        check_owner(product, actor)
        if data is None:
            product.status = ProductStatus.ARCHIVED
        else:
            # Leave headroom for every reservation that can still be canceled.
            reserved = await session.scalar(
                select(func.coalesce(func.sum(OrderItem.quantity), 0))
                .join(Order, Order.id == OrderItem.order_id)
                .where(
                    OrderItem.product_id == product.id,
                    Order.status.in_(["CREATED", "PAYMENT_PENDING"]),
                )
            )
            if data.stock + reserved > 2147483647:
                invalid_field(
                    "body.stock", "Stock plus cancellable reservations exceeds integer capacity"
                )
            for key, value in data.model_dump().items():
                setattr(product, key, value)
        await session.flush()
        await session.refresh(product)
        return ProductResponse.model_validate(product)
