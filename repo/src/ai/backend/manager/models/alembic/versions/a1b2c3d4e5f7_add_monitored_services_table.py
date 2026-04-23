"""add monitored_services table for health check monitor

Revision ID: a1b2c3d4e5f7
Revises: (latest)
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f7"
down_revision = None  # Will be set to the actual latest revision
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
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("last_check_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer, nullable=True),
        sa.Column("last_latency_ms", sa.Float, nullable=True),
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
