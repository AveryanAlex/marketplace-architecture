from datetime import datetime
from decimal import Decimal
from uuid import UUID

from marketplace_api.models.order_status import OrderStatus
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, Identified, Timestamped


class Order(Identified, Timestamped, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount >= 0 AND discount_amount >= 0", name="nonnegative_amounts"),
        Index(
            "uq_orders_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('CREATED', 'PAYMENT_PENDING')"),
        ),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"))
    promo_code_id: Mapped[UUID | None] = mapped_column(ForeignKey("promo_codes.id"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="OrderItem.id",
    )


class OrderItem(Identified, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity BETWEEN 1 AND 999", name="quantity_bounds"),
        CheckConstraint("price_at_order > 0", name="positive_price"),
        Index("uq_order_items_order_product", "order_id", "product_id", unique=True),
    )
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price_at_order: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    order: Mapped[Order] = relationship(back_populates="items")


class UserOperation(Identified, Base):
    __tablename__ = "user_operations"
    __table_args__ = (Index("ix_user_operations_last", "user_id", "operation_type", "created_at"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation_type: Mapped[str] = mapped_column(
        Enum("CREATE_ORDER", "UPDATE_ORDER", name="operation_type")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
