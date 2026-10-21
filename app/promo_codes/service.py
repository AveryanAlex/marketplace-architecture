from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from marketplace_api.models.promo_code_response import PromoCodeResponse
from sqlalchemy.dialects.postgresql import insert

from app.auth.security import require_role
from app.core.errors import APIError
from app.promo_codes.models import PromoCode

CENT = Decimal("0.01")


def applicable(promo, amount, now=None, own_use=False):
    now = now or datetime.now(UTC)
    return (
        promo.active
        and promo.valid_from <= now <= promo.valid_until
        and promo.current_uses < promo.max_uses + int(own_use)
        and amount >= promo.min_order_amount
    )


def discount(promo, amount):
    if promo.discount_type == "PERCENTAGE":
        # Literal interpretation of the assignment: >70% means full price.
        value = amount * promo.discount_value / 100 if promo.discount_value <= 70 else Decimal(0)
    else:
        value = min(promo.discount_value, amount)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


async def create(session, actor, data):
    require_role(actor, "SELLER", "ADMIN")
    async with session.begin():
        promo_id = await session.scalar(
            insert(PromoCode)
            .values(**data.model_dump())
            .on_conflict_do_nothing(index_elements=["code"])
            .returning(PromoCode.id)
        )
        if promo_id is None:
            raise APIError(422, "PROMO_CODE_INVALID", "Promo code already exists")
        promo = await session.get(PromoCode, promo_id)
        return PromoCodeResponse.model_validate(promo)
