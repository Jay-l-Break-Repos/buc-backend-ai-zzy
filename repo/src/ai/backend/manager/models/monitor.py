"""
Data model for the health check monitor.

Each monitored service stores:
- id: unique UUID identifier
- url: the endpoint URL to monitor
- name: optional human-readable label (defaults to the URL)
- last_check_time: timestamp of the most recent health check (None if never checked)
- last_status_code: HTTP response status code from the last check (None if never checked)
- last_latency_ms: round-trip latency in milliseconds from the last check (None if never checked)
- status: "up" | "down" | None (None means never checked)
- created_at: timestamp when the service was registered
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional


class MonitoredService:
    """
    In-memory representation of a single monitored service entry.

    All fields that relate to the most recent health check result are
    initialised to ``None`` and are populated once the first poll completes.
    """

    def __init__(
        self,
        *,
        id: uuid.UUID,
        url: str,
        name: str,
        created_at: datetime,
        last_check_time: Optional[datetime] = None,
        last_status_code: Optional[int] = None,
        last_latency_ms: Optional[float] = None,
        status: Optional[str] = None,
    ) -> None:
        self.id = id
        self.url = url
        self.name = name
        self.created_at = created_at
        self.last_check_time = last_check_time
        self.last_status_code = last_status_code
        self.last_latency_ms = last_latency_ms
        self.status = status

    def to_dict(self) -> dict:
        """Serialise the service to a plain dict suitable for JSON responses."""
        return {
            "id": str(self.id),
            "url": self.url,
            "name": self.name,
            "last_check_time": (
                self.last_check_time.isoformat() if self.last_check_time is not None else None
            ),
            "last_status_code": self.last_status_code,
            "last_latency_ms": self.last_latency_ms,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def create(cls, url: str, name: Optional[str] = None) -> "MonitoredService":
        """
        Factory method that creates a new ``MonitoredService`` with a fresh UUID
        and the current UTC timestamp.  The *name* defaults to the URL when not
        provided.
        """
        now = datetime.now(tz=timezone.utc)
        return cls(
            id=uuid.uuid4(),
            url=url,
            name=name if name else url,
            created_at=now,
        )


class InMemoryServiceStore:
    """
    Thread-safe (within a single asyncio event loop) in-memory store for
    :class:`MonitoredService` instances, keyed by their UUID.

    This store is intentionally simple — it is designed to be replaced by a
    persistent backend (e.g. a PostgreSQL table) in a later step.
    """

    def __init__(self) -> None:
        self._services: dict[uuid.UUID, MonitoredService] = {}

    def add(self, service: MonitoredService) -> None:
        """Register a new service in the store."""
        self._services[service.id] = service

    def get(self, service_id: uuid.UUID) -> Optional[MonitoredService]:
        """Return the service with the given *service_id*, or ``None``."""
        return self._services.get(service_id)

    def list(self) -> list[MonitoredService]:
        """Return all services ordered by creation time (oldest first)."""
        return sorted(self._services.values(), key=lambda s: s.created_at)

    def remove(self, service_id: uuid.UUID) -> bool:
        """
        Remove the service identified by *service_id*.

        Returns ``True`` if the service existed and was removed, ``False`` if
        no service with that ID was found.
        """
        if service_id in self._services:
            del self._services[service_id]
            return True
        return False

    def update_health(
        self,
        service_id: uuid.UUID,
        *,
        status_code: Optional[int],
        latency_ms: float,
        checked_at: datetime,
    ) -> bool:
        """
        Update the health-check result fields for the service identified by
        *service_id*.

        ``status_code`` is ``None`` when the request failed entirely (e.g.
        connection error or timeout), in which case the service is marked
        ``"down"``.  Any 2xx status code marks the service ``"up"``; all other
        codes mark it ``"down"``.

        Returns ``True`` if the service was found and updated, ``False``
        otherwise.
        """
        svc = self._services.get(service_id)
        if svc is None:
            return False
        svc.last_check_time = checked_at
        svc.last_status_code = status_code
        svc.last_latency_ms = round(latency_ms, 3)
        if status_code is not None and 200 <= status_code < 300:
            svc.status = "up"
        else:
            svc.status = "down"
        return True

    def __len__(self) -> int:
        return len(self._services)
