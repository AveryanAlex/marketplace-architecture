from decimal import Decimal
from uuid import UUID

from marketplace_api.models.product_status import ProductStatus
from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, Identified, Timestamped


class Product(Identified, Timestamped, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="positive_price"),
        CheckConstraint("stock >= 0", name="nonnegative_stock"),
        CheckConstraint("length(name) BETWEEN 1 AND 255", name="name_length"),
        CheckConstraint("length(category) BETWEEN 1 AND 100", name="category_length"),
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(4000))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(100))
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, name="product_status"), index=True
    )
    seller_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
