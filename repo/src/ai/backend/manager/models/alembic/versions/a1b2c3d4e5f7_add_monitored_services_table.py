"""add monitored_services table for health check monitor

Revision ID: a1b2c3d4e5f7
Revises: fa3dd7f77c19
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f7"
down_revision = "fa3dd7f77c19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitored_services",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("last_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("monitored_services")
