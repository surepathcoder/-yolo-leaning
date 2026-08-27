"""The realtime WebSocket endpoint.

Authentication is the awkward part. Browsers cannot set an ``Authorization``
header on a WebSocket handshake, so the token has to arrive another way. The
options and why this one:

* **Query parameter** -- simple, but URLs end up in access logs and proxy
  logs, which is where credentials go to be leaked.
* **Cookie** -- works, but requires cookie auth everywhere and opens CSRF
  questions the REST API does not currently have.
* **First message after connect** -- the socket is accepted, the client sends
  its token as the first frame, and the server closes it if the token is bad
  or does not arrive in time. Nothing sensitive touches a URL.

The third is used here. An unauthenticated socket is closed after five
seconds, so an attacker cannot hold connections open cheaply.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import TokenError, decode_token
from app.db.session import get_db
from app.models import Camera, User
from app.services.auth import build_scope
from app.websocket.manager import Connection, MessageType, manager

log = get_logger(__name__)

router = APIRouter()

#: How long an accepted socket may stay unauthenticated.
AUTH_TIMEOUT_SECONDS = 5.0

#: Server-side keepalive. Idle WebSockets are killed by intermediate proxies
#: and mobile networks well inside a minute; a ping keeps them alive and lets
#: the server notice a dead peer.
PING_INTERVAL_SECONDS = 25.0


def _authenticate(session: Session, token: str) -> User | None:
    """Resolve a user from an access token, or ``None``."""
    try:
        payload = decode_token(token, expected_type="access")
    except TokenError:
        return None

    user = session.get(User, payload.subject)
    if user is None or user.deleted_at is not None or not user.is_active:
        return None
    if user.organization is not None and not user.organization.is_active:
        return None
    return user


def _resolve_camera_filter(
    session: Session, user: User, camera_ids: list[str]
) -> frozenset[uuid.UUID]:
    """Narrow a socket to specific cameras, verifying the caller owns them.

    A client asking for cameras it cannot see gets an empty filter for those,
    never someone else's feed.
    """
    if not camera_ids:
        return frozenset()

    scope = build_scope(user)
    wanted: list[uuid.UUID] = []
    for raw in camera_ids[:50]:
        try:
            wanted.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    if not wanted:
        return frozenset()

    from sqlalchemy import select

    allowed = session.scalars(
        scope.apply(
            select(Camera.id).where(Camera.id.in_(wanted), Camera.deleted_at.is_(None)),
            Camera,
        )
    ).all()
    return frozenset(allowed)


#: Injected rather than built from the global factory, so the same dependency
#: override the REST tests use also applies to WebSocket connections.
WsSession = Annotated[Session, Depends(get_db)]


@router.websocket("/ws/realtime")
async def realtime(websocket: WebSocket, session: WsSession) -> None:
    """Live events, alerts and camera status for the signed-in user.

    Protocol::

        server: (accepts)
        client: {"type": "auth", "token": "<access token>",
                 "camera_ids": ["..."]}          # camera_ids optional
        server: {"type": "connected", "connection_id": "..."}
        server: {"type": "event"|"alert"|"camera_status", ...}
        client: {"type": "ping"} -> server: {"type": "pong"}
    """
    await websocket.accept()
    connection: Connection | None = None

    try:
        # --- authentication --------------------------------------------------
        try:
            async with asyncio.timeout(AUTH_TIMEOUT_SECONDS):
                raw = await websocket.receive_text()
        except (TimeoutError, WebSocketDisconnect):
            await websocket.close(code=4401, reason="authentication timeout")
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.close(code=4400, reason="malformed auth message")
            return

        token = payload.get("token", "")
        try:
            user = _authenticate(session, token) if token else None
        except Exception as exc:
            # A database problem during authentication must close the socket
            # cleanly rather than leaving the client waiting forever.
            log.error("websocket_auth_error", error=str(exc)[:200])
            await websocket.close(code=1011, reason="authentication unavailable")
            return
        if user is None:
            log.info("websocket_auth_rejected")
            await websocket.close(code=4401, reason="authentication failed")
            return

        scope = build_scope(user)
        if not scope.has(Permission.EVENT_READ):
            await websocket.close(code=4403, reason="insufficient permissions")
            return

        camera_filter = _resolve_camera_filter(session, user, payload.get("camera_ids") or [])

        connection = Connection(
            socket=websocket,
            connection_id=uuid.uuid4().hex,
            user_id=user.id,
            organization_id=user.organization_id,
            is_platform_operator=scope.is_platform_operator,
            store_ids=scope.store_ids,
            camera_filter=camera_filter,
        )
        await manager.connect(connection)

        # --- serve -----------------------------------------------------------
        keepalive = asyncio.create_task(_keepalive(websocket))
        try:
            while True:
                message = await websocket.receive_text()
                try:
                    parsed = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if parsed.get("type") == MessageType.PING:
                    await websocket.send_text(json.dumps({"type": MessageType.PONG}))
                elif parsed.get("type") == "set_camera_filter":
                    # Re-resolved against the caller's scope on every change,
                    # so a client cannot widen its own access mid-session.
                    connection.camera_filter = _resolve_camera_filter(
                        session, user, parsed.get("camera_ids") or []
                    )
        finally:
            keepalive.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("websocket_error", error=str(exc)[:200])
    finally:
        if connection is not None:
            await manager.disconnect(connection.connection_id)


async def _keepalive(websocket: WebSocket) -> None:
    """Ping periodically so proxies do not silently drop an idle socket."""
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            await websocket.send_text(json.dumps({"type": MessageType.PING}))
    except (asyncio.CancelledError, Exception):
        return
