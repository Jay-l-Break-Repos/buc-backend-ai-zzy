"""
Data model for the health check monitor.

Each monitored service stores:
- URL
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
    sa.Column("url", sa.String(2048), nullable=False),
    sa.Column("last_check_time", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_status_code", sa.Integer, nullable=True),
    sa.Column("last_latency_ms", sa.Float, nullable=True),
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
        url: str,
        last_check_time: Optional[datetime],
        last_status_code: Optional[int],
        last_latency_ms: Optional[float],
        status: Optional[str],
        created_at: datetime,
    ) -> None:
        self.id = id
        self.url = url
        self.last_check_time = last_check_time
        self.last_status_code = last_status_code
        self.last_latency_ms = last_latency_ms
        self.status = status
        self.created_at = created_at

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "url": self.url,
            "last_check_time": (
                self.last_check_time.isoformat() if self.last_check_time else None
            ),
            "last_status_code": self.last_status_code,
            "last_latency_ms": self.last_latency_ms,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_row(cls, row) -> "MonitoredServiceRow":
        return cls(
            id=row["id"],
            url=row["url"],
            last_check_time=row["last_check_time"],
            last_status_code=row["last_status_code"],
            last_latency_ms=row["last_latency_ms"],
            status=row["status"],
            created_at=row["created_at"],
        )
