"""initial marketplace schema

Revision ID: f9a957f5c5df
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a957f5c5df"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column(
            "discount_type",
            sa.Enum("PERCENTAGE", "FIXED_AMOUNT", name="discount_type"),
            nullable=False,
        ),
        sa.Column("discount_value", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("min_order_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("current_uses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint("code ~ '^[A-Z0-9_]{4,20}$'", name=op.f("ck_promo_codes_code_format")),
        sa.CheckConstraint(
            "discount_type != 'PERCENTAGE' OR discount_value <= 100",
            name=op.f("ck_promo_codes_percentage"),
        ),
        sa.CheckConstraint("discount_value > 0", name=op.f("ck_promo_codes_positive_discount")),
        sa.CheckConstraint(
            "max_uses > 0 AND current_uses BETWEEN 0 AND max_uses",
            name=op.f("ck_promo_codes_usage_bounds"),
        ),
        sa.CheckConstraint(
            "min_order_amount >= 0", name=op.f("ck_promo_codes_nonnegative_minimum")
        ),
        sa.CheckConstraint("valid_until > valid_from", name=op.f("ck_promo_codes_valid_interval")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_promo_codes_code")),
    )
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("USER", "SELLER", "ADMIN", name="user_role"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "orders",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "PAYMENT_PENDING",
                "PAID",
                "SHIPPED",
                "COMPLETED",
                "CANCELED",
                name="order_status",
            ),
            nullable=False,
        ),
        sa.Column("promo_code_id", sa.Uuid(), nullable=True),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=12, scale=2), server_default="0", nullable=False
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_amount >= 0 AND discount_amount >= 0", name=op.f("ck_orders_nonnegative_amounts")
        ),
        sa.ForeignKeyConstraint(
            ["promo_code_id"], ["promo_codes.id"], name=op.f("fk_orders_promo_code_id_promo_codes")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(
        "uq_orders_active_user",
        "orders",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CREATED', 'PAYMENT_PENDING')"),
    )
    op.create_table(
        "products",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=4000), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "INACTIVE", "ARCHIVED", name="product_status"),
            nullable=False,
        ),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(category) BETWEEN 1 AND 100", name=op.f("ck_products_category_length")
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 255", name=op.f("ck_products_name_length")),
        sa.CheckConstraint("price > 0", name=op.f("ck_products_positive_price")),
        sa.CheckConstraint("stock >= 0", name=op.f("ck_products_nonnegative_stock")),
        sa.ForeignKeyConstraint(
            ["seller_id"], ["users.id"], name=op.f("fk_products_seller_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index(op.f("ix_products_seller_id"), "products", ["seller_id"], unique=False)
    op.create_index(op.f("ix_products_status"), "products", ["status"], unique=False)
    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_refresh_tokens_user_id_users")
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_refresh_tokens")),
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "user_operations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "operation_type",
            sa.Enum("CREATE_ORDER", "UPDATE_ORDER", name="operation_type"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_operations_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_operations")),
    )
    op.create_index(
        "ix_user_operations_last",
        "user_operations",
        ["user_id", "operation_type", "created_at"],
        unique=False,
    )
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_at_order", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint("price_at_order > 0", name=op.f("ck_order_items_positive_price")),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 999", name=op.f("ck_order_items_quantity_bounds")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_order_items_product_id_products")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_items_product_id"), "order_items", ["product_id"], unique=False)
    op.create_index(
        "uq_order_items_order_product", "order_items", ["order_id", "product_id"], unique=True
    )
    op.execute("""
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$
    """)
    for table in ("products", "orders"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    op.drop_index("uq_order_items_order_product", table_name="order_items")
    op.drop_index(op.f("ix_order_items_product_id"), table_name="order_items")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_user_operations_last", table_name="user_operations")
    op.drop_table("user_operations")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(op.f("ix_products_status"), table_name="products")
    op.drop_index(op.f("ix_products_seller_id"), table_name="products")
    op.drop_table("products")
    op.drop_index(
        "uq_orders_active_user",
        table_name="orders",
        postgresql_where=sa.text("status IN ('CREATED', 'PAYMENT_PENDING')"),
    )
    op.drop_table("orders")
    op.drop_table("users")
    op.drop_table("promo_codes")
    op.execute("DROP FUNCTION set_updated_at()")
    for name in ("operation_type", "product_status", "order_status", "user_role", "discount_type"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
