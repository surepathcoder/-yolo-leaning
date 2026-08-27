"""Cross-process realtime fan-out over Redis pub/sub.

A vision worker persists an event in one process; the operator's WebSocket
lives in another. Without a shared bus, only connections that happen to share
a process would ever see it.

Publishing is **synchronous and best-effort**: it is called from ordinary
request and worker code, and a Redis outage must degrade realtime updates, not
break event ingestion. The dashboard falls back to polling; the events are
still in the database either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.redis_client import get_redis
from app.websocket.manager import REALTIME_CHANNEL, manager

log = get_logger(__name__)


def publish(message: dict[str, Any]) -> bool:
    """Publish a realtime message to every API process.

    Returns whether it reached Redis. A ``False`` here is not an error the
    caller should propagate -- the event is already persisted, and realtime
    delivery is an enhancement over polling.
    """
    manager.messages_published += 1
    try:
        get_redis().publish(REALTIME_CHANNEL, json.dumps(message, default=str))
        return True
    except Exception as exc:
        log.warning(
            "realtime_publish_failed",
            error=str(exc)[:200],
            detail="dashboard will fall back to polling",
        )
        return False


async def publish_async(message: dict[str, Any]) -> bool:
    """Publish without blocking the event loop.

    The synchronous Redis client would stall every other WebSocket on the
    process while it waits on the socket.
    """
    return await asyncio.to_thread(publish, message)


class RealtimeSubscriber:
    """Relays messages from Redis onto this process's WebSocket connections.

    One instance per API process, started and stopped with the application.
    Reconnects on its own: a Redis restart must not permanently silence the
    dashboards served by this process.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.messages_relayed = 0
        self.reconnects = 0
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="realtime-subscriber")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.connected = False

    async def _run(self) -> None:
        settings = get_settings()
        delay = 1.0

        while not self._stop.is_set():
            client = None
            try:
                client = aioredis.from_url(
                    settings.REDIS_URL, decode_responses=True, socket_timeout=30
                )
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(REALTIME_CHANNEL)
                self.connected = True
                self.last_error = None
                delay = 1.0
                log.info("realtime_subscriber_connected", channel=REALTIME_CHANNEL)

                async for raw in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if raw is None or raw.get("type") != "message":
                        continue
                    try:
                        message = json.loads(raw["data"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        # A malformed message from an unknown publisher must
                        # not kill the subscriber for everyone else.
                        log.warning("realtime_message_malformed")
                        continue
                    await manager.broadcast(message)
                    self.messages_relayed += 1

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                self.last_error = str(exc)[:200]
                log.warning(
                    "realtime_subscriber_reconnecting",
                    error=self.last_error,
                    delay_seconds=delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except TimeoutError:
                    # Backoff, capped: a long Redis outage must not turn into
                    # a reconnect storm when it comes back.
                    delay = min(delay * 2, 30.0)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "messages_relayed": self.messages_relayed,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


#: Process-wide subscriber.
subscriber = RealtimeSubscriber()
