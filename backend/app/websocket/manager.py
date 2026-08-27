"""WebSocket connection management and realtime fan-out.

The dashboard must update without a refresh. Two things make that harder than
opening a socket:

**Multiple API processes.** A vision worker persists an event in one process
while the operator's socket lives in another. An in-process queue would
silently deliver to only the lucky third of users. Redis pub/sub is the
decoupling point: publishers do not know who is listening, and any process can
serve any connection.

**Tenancy.** A socket is scoped to one organization and, for a
store-restricted user, to their stores. Broadcasting to a channel and letting
the client filter would ship another tenant's events to their browser, where
anyone can read them in devtools. Filtering therefore happens **server side,
before the send**.

Redis being unavailable degrades to local-only delivery rather than failing:
an operator watching one process still sees their events, and ``/health``
reports the degradation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

from app.core.logging import get_logger

log = get_logger(__name__)

#: The Redis channel every process publishes realtime messages to.
REALTIME_CHANNEL = "visionguard:realtime"

#: Messages queued per connection before the slowest client is disconnected.
#: A browser on a stalled mobile connection must not be able to grow the
#: server's memory without bound.
MAX_QUEUE_PER_CONNECTION = 100


class MessageType:
    """Realtime message kinds, mirrored by the frontend's TypeScript union."""

    EVENT = "event"
    ALERT = "alert"
    CAMERA_STATUS = "camera_status"
    METRICS = "metrics"
    PING = "ping"
    PONG = "pong"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class Connection:
    """One live dashboard socket and what it is allowed to see."""

    socket: WebSocket
    connection_id: str
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    is_platform_operator: bool = False
    #: Empty means every store in the organization.
    store_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    #: Optional client-side narrowing, e.g. one camera's live view.
    camera_filter: frozenset[uuid.UUID] = field(default_factory=frozenset)
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    messages_sent: int = 0

    def may_receive(self, message: dict[str, Any]) -> bool:
        """Whether this connection is allowed to see a message.

        Enforced before sending, never by the client.
        """
        if self.is_platform_operator:
            return True

        organization_id = message.get("organization_id")
        if organization_id is None:
            return False
        if str(self.organization_id) != str(organization_id):
            return False

        if self.store_ids:
            store_id = message.get("store_id")
            # A message with no store (an organization-wide notice) is allowed
            # through; anything attributed to a store must be one of theirs.
            if store_id is not None and uuid.UUID(str(store_id)) not in self.store_ids:
                return False

        if self.camera_filter:
            camera_id = message.get("camera_id")
            if camera_id is None or uuid.UUID(str(camera_id)) not in self.camera_filter:
                return False

        return True


class ConnectionManager:
    """Tracks live sockets and delivers messages to the right ones."""

    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._by_organization: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self.messages_published = 0
        self.messages_delivered = 0
        self.slow_clients_dropped = 0

    # ------------------------------------------------------------- lifecycle
    async def connect(self, connection: Connection) -> None:
        async with self._lock:
            self._connections[connection.connection_id] = connection
            key = str(connection.organization_id)
            self._by_organization[key].add(connection.connection_id)

        log.info(
            "websocket_connected",
            connection_id=connection.connection_id,
            user_id=str(connection.user_id),
            organization_id=str(connection.organization_id),
            total_connections=len(self._connections),
        )
        await self._send(
            connection,
            {
                "type": MessageType.CONNECTED,
                "connection_id": connection.connection_id,
                "server_time": datetime.now(UTC).isoformat(),
            },
        )

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            connection = self._connections.pop(connection_id, None)
            if connection is not None:
                key = str(connection.organization_id)
                self._by_organization[key].discard(connection_id)
                if not self._by_organization[key]:
                    del self._by_organization[key]

        if connection is not None:
            log.info(
                "websocket_disconnected",
                connection_id=connection_id,
                messages_sent=connection.messages_sent,
                duration_seconds=round(
                    (datetime.now(UTC) - connection.connected_at).total_seconds(), 1
                ),
            )

    # -------------------------------------------------------------- delivery
    async def _send(self, connection: Connection, payload: dict[str, Any]) -> bool:
        """Send to one socket. Returns False if the socket is gone."""
        try:
            await connection.socket.send_text(json.dumps(payload, default=str))
            connection.messages_sent += 1
            return True
        except Exception:
            # A closed or broken socket is routine, not an error worth a
            # stack trace: the client navigated away or lost signal.
            return False

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Deliver to every connection permitted to see it. Returns the count.

        Each send is given a short timeout. A client that cannot keep up is
        disconnected rather than allowed to block delivery to everyone else --
        one stalled mobile connection must not freeze the operations room.
        """
        async with self._lock:
            candidates = list(self._connections.values())

        delivered = 0
        casualties: list[str] = []

        for connection in candidates:
            if not connection.may_receive(message):
                continue
            try:
                async with asyncio.timeout(2.0):
                    ok = await self._send(connection, message)
            except TimeoutError:
                self.slow_clients_dropped += 1
                log.warning(
                    "websocket_slow_client_dropped",
                    connection_id=connection.connection_id,
                )
                casualties.append(connection.connection_id)
                continue
            if ok:
                delivered += 1
            else:
                casualties.append(connection.connection_id)

        for connection_id in casualties:
            await self.disconnect(connection_id)

        self.messages_delivered += delivered
        return delivered

    async def send_to_user(self, user_id: uuid.UUID, message: dict[str, Any]) -> int:
        async with self._lock:
            targets = [
                connection
                for connection in self._connections.values()
                if connection.user_id == user_id
            ]
        delivered = 0
        for connection in targets:
            if await self._send(connection, message):
                delivered += 1
        return delivered

    # ------------------------------------------------------------- accessors
    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def organization_connection_count(self, organization_id: uuid.UUID) -> int:
        return len(self._by_organization.get(str(organization_id), set()))

    def stats(self) -> dict[str, Any]:
        return {
            "connections": self.connection_count,
            "organizations": len(self._by_organization),
            "messages_published": self.messages_published,
            "messages_delivered": self.messages_delivered,
            "slow_clients_dropped": self.slow_clients_dropped,
        }

    async def close_all(self) -> None:
        """Close every socket. Called on shutdown."""
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            self._by_organization.clear()
        for connection in connections:
            # A socket the client already dropped raises here; that is the
            # normal case during shutdown, not a problem.
            with contextlib.suppress(Exception):
                await connection.socket.close(code=1001)


#: Process-wide manager. One per API process.
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def event_message(event) -> dict[str, Any]:
    """Realtime payload for a new event.

    Carries ``is_inference`` and ``confidence_level`` so the dashboard renders
    a conclusion differently from a measurement in the live feed too, not just
    on the timeline.
    """
    return {
        "type": MessageType.EVENT,
        "organization_id": str(event.organization_id),
        "store_id": str(event.store_id) if event.store_id else None,
        "camera_id": str(event.camera_id) if event.camera_id else None,
        "data": {
            "id": str(event.id),
            "event_type": event.event_type.value,
            "severity": event.severity.value,
            "is_inference": event.is_inference,
            "confidence_level": (event.confidence_level.value if event.confidence_level else None),
            "zone_id": str(event.zone_id) if event.zone_id else None,
            "track_id": event.track_id,
            "occurred_at": event.occurred_at.isoformat(),
            "summary": event.summary,
            "has_evidence": bool(event.evidence),
        },
    }


def alert_message(alert) -> dict[str, Any]:
    return {
        "type": MessageType.ALERT,
        "organization_id": str(alert.organization_id),
        "store_id": str(alert.store_id) if alert.store_id else None,
        "camera_id": (
            str(alert.event.camera_id)
            if alert.event is not None and alert.event.camera_id
            else None
        ),
        "data": {
            "id": str(alert.id),
            "event_id": str(alert.event_id),
            "title": alert.title,
            "message": alert.message,
            "severity": alert.severity.value,
            "status": alert.status.value,
            "is_inference": alert.event.is_inference if alert.event else False,
            "created_at": alert.created_at.isoformat(),
        },
    }


def camera_status_message(
    camera_id: uuid.UUID,
    organization_id: uuid.UUID,
    store_id: uuid.UUID | None,
    status: str,
    *,
    error: str | None = None,
    measured_fps: float | None = None,
) -> dict[str, Any]:
    return {
        "type": MessageType.CAMERA_STATUS,
        "organization_id": str(organization_id),
        "store_id": str(store_id) if store_id else None,
        "camera_id": str(camera_id),
        "data": {
            "camera_id": str(camera_id),
            "status": status,
            # Safe by contract: camera errors never carry credentials.
            "error": error,
            "measured_fps": measured_fps,
            "at": datetime.now(UTC).isoformat(),
        },
    }
