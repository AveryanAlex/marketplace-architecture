from datetime import datetime
from decimal import Decimal

from marketplace_api.models.discount_type import DiscountType
from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, Identified


class PromoCode(Identified, Base):
    __tablename__ = "promo_codes"
    __table_args__ = (
        CheckConstraint("discount_value > 0", name="positive_discount"),
        CheckConstraint("min_order_amount >= 0", name="nonnegative_minimum"),
        CheckConstraint(
            "max_uses > 0 AND current_uses BETWEEN 0 AND max_uses", name="usage_bounds"
        ),
        CheckConstraint("valid_until > valid_from", name="valid_interval"),
        CheckConstraint(
            "discount_type != 'PERCENTAGE' OR discount_value <= 100", name="percentage"
        ),
        CheckConstraint("code ~ '^[A-Z0-9_]{4,20}$'", name="code_format"),
    )
    code: Mapped[str] = mapped_column(String(20), unique=True)
    discount_type: Mapped[DiscountType] = mapped_column(Enum(DiscountType, name="discount_type"))
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    min_order_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_uses: Mapped[int] = mapped_column(Integer)
    current_uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
