"""
Data model for the health check monitor.

Each monitored service stores:
- URL
- Name (human-readable label)
- Last check time
- HTTP response status code
- Latency in milliseconds
- Status (up/down)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from .base import metadata

# Table definition for monitored services
monitored_services = sa.Table(
    "monitored_services",
    metadata,
    sa.Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("uuid_generate_v4()"),
    ),
    sa.Column("name", sa.String(256), nullable=False),
    sa.Column("url", sa.String(2048), nullable=False),
    sa.Column("last_check", sa.DateTime(timezone=True), nullable=True),
    sa.Column("status_code", sa.Integer, nullable=True),
    sa.Column("latency_ms", sa.Float, nullable=True),
    sa.Column("status", sa.String(16), nullable=True),  # "up" | "down" | None (never checked)
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
)


class MonitoredServiceRow:
    """
    A plain Python representation of a monitored service row.
    """

    def __init__(
        self,
        id: uuid.UUID,
        name: str,
        url: str,
        last_check: Optional[datetime],
        status_code: Optional[int],
        latency_ms: Optional[float],
        status: Optional[str],
        created_at: datetime,
    ) -> None:
        self.id = id
        self.name = name
        self.url = url
        self.last_check = last_check
        self.status_code = status_code
        self.latency_ms = latency_ms
        self.status = status
        self.created_at = created_at

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "url": self.url,
            "last_check": (
                self.last_check.isoformat() if self.last_check else None
            ),
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_row(cls, row) -> "MonitoredServiceRow":
        return cls(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            last_check=row["last_check"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            status=row["status"],
            created_at=row["created_at"],
        )
