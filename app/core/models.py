"""Import every table for Alembic metadata registration."""

from app.auth.models import RefreshToken, User
from app.orders.models import Order, OrderItem, UserOperation
from app.products.models import Product
from app.promo_codes.models import PromoCode

__all__ = ["RefreshToken", "User", "Order", "OrderItem", "UserOperation", "Product", "PromoCode"]
