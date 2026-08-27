"""Milestone 38 -- realtime delivery.

The security property under test: a WebSocket must never deliver another
tenant's events. Filtering happens server side, before the send, because a
message that reaches the browser is readable in devtools no matter what the
client-side code does with it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.security import hash_password
from app.models.enums import EventSeverity, EventType, UserRole
from app.websocket.manager import (
    Connection,
    ConnectionManager,
    MessageType,
    alert_message,
    camera_status_message,
    event_message,
)
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"


class FakeSocket:
    """Captures what would have been sent, and can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise ConnectionError("socket is gone")
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def make_connection(
    *,
    organization_id: uuid.UUID | None = None,
    store_ids: frozenset[uuid.UUID] = frozenset(),
    camera_filter: frozenset[uuid.UUID] = frozenset(),
    is_platform_operator: bool = False,
    fail: bool = False,
) -> Connection:
    return Connection(
        socket=FakeSocket(fail=fail),  # type: ignore[arg-type]
        connection_id=uuid.uuid4().hex,
        user_id=uuid.uuid4(),
        organization_id=organization_id or uuid.uuid4(),
        is_platform_operator=is_platform_operator,
        store_ids=store_ids,
        camera_filter=camera_filter,
    )


def message_for(
    organization_id: uuid.UUID,
    *,
    store_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
) -> dict:
    return {
        "type": MessageType.EVENT,
        "organization_id": str(organization_id),
        "store_id": str(store_id) if store_id else None,
        "camera_id": str(camera_id) if camera_id else None,
        "data": {"summary": "Person entered zone"},
    }


# ------------------------------------------------------- permission filtering
def test_a_connection_receives_its_own_organizations_events():
    org = uuid.uuid4()
    connection = make_connection(organization_id=org)
    assert connection.may_receive(message_for(org)) is True


def test_a_connection_never_receives_another_organizations_events():
    """The guarantee that, if broken, ends the product."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    connection = make_connection(organization_id=mine)
    assert connection.may_receive(message_for(theirs)) is False


def test_a_message_without_an_organization_is_refused():
    """Fail closed: an unattributed message is not broadcast to everyone."""
    connection = make_connection()
    assert connection.may_receive({"type": "event", "data": {}}) is False


def test_platform_operator_receives_everything():
    connection = make_connection(is_platform_operator=True)
    assert connection.may_receive(message_for(uuid.uuid4())) is True


def test_store_restricted_connection_only_gets_its_stores():
    org = uuid.uuid4()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    connection = make_connection(organization_id=org, store_ids=frozenset({mine}))

    assert connection.may_receive(message_for(org, store_id=mine)) is True
    assert connection.may_receive(message_for(org, store_id=theirs)) is False


def test_store_restricted_connection_still_gets_organization_wide_messages():
    """A notice with no store attached is not store-specific."""
    org = uuid.uuid4()
    connection = make_connection(organization_id=org, store_ids=frozenset({uuid.uuid4()}))
    assert connection.may_receive(message_for(org, store_id=None)) is True


def test_camera_filter_narrows_the_stream():
    """A live-view page subscribes to one camera, not the whole estate."""
    org = uuid.uuid4()
    watched, other = uuid.uuid4(), uuid.uuid4()
    connection = make_connection(organization_id=org, camera_filter=frozenset({watched}))
    assert connection.may_receive(message_for(org, camera_id=watched)) is True
    assert connection.may_receive(message_for(org, camera_id=other)) is False
    assert connection.may_receive(message_for(org, camera_id=None)) is False


# ------------------------------------------------------------------ broadcast
@pytest.mark.asyncio
async def test_broadcast_reaches_only_permitted_connections():
    manager = ConnectionManager()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    a1 = make_connection(organization_id=org_a)
    a2 = make_connection(organization_id=org_a)
    b1 = make_connection(organization_id=org_b)
    for connection in (a1, a2, b1):
        await manager.connect(connection)

    delivered = await manager.broadcast(message_for(org_a))
    assert delivered == 2

    # The "connected" handshake is the first message on each socket.
    assert len(a1.socket.sent) == 2
    assert len(b1.socket.sent) == 1
    assert b1.socket.sent[0]["type"] == MessageType.CONNECTED


@pytest.mark.asyncio
async def test_a_dead_socket_is_removed_not_retried():
    manager = ConnectionManager()
    org = uuid.uuid4()
    alive = make_connection(organization_id=org)
    dead = make_connection(organization_id=org, fail=True)
    await manager.connect(alive)
    await manager.connect(dead)

    delivered = await manager.broadcast(message_for(org))
    assert delivered == 1
    assert manager.connection_count == 1


@pytest.mark.asyncio
async def test_connect_and_disconnect_track_counts():
    manager = ConnectionManager()
    org = uuid.uuid4()
    connection = make_connection(organization_id=org)

    await manager.connect(connection)
    assert manager.connection_count == 1
    assert manager.organization_connection_count(org) == 1

    await manager.disconnect(connection.connection_id)
    assert manager.connection_count == 0
    assert manager.organization_connection_count(org) == 0


@pytest.mark.asyncio
async def test_disconnecting_an_unknown_connection_is_harmless():
    manager = ConnectionManager()
    await manager.disconnect("not-a-real-connection")
    assert manager.connection_count == 0


@pytest.mark.asyncio
async def test_send_to_user_targets_every_device():
    manager = ConnectionManager()
    org = uuid.uuid4()
    user = uuid.uuid4()

    phone = make_connection(organization_id=org)
    laptop = make_connection(organization_id=org)
    phone.user_id = laptop.user_id = user
    other = make_connection(organization_id=org)
    for connection in (phone, laptop, other):
        await manager.connect(connection)

    delivered = await manager.send_to_user(user, {"type": "test"})
    assert delivered == 2


@pytest.mark.asyncio
async def test_close_all_clears_every_connection():
    manager = ConnectionManager()
    for _ in range(3):
        await manager.connect(make_connection())
    await manager.close_all()
    assert manager.connection_count == 0


# ------------------------------------------------------------ message shapes
def test_event_message_carries_the_inference_flag(db_session):
    """The live feed must distinguish a conclusion from a measurement too."""
    org = f.make_organization(db_session)
    camera = f.make_camera(db_session, f.make_store(db_session, org))
    event = f.make_event(
        db_session,
        camera,
        event_type=EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        severity=EventSeverity.MEDIUM,
        is_inference=True,
    )
    message = event_message(event)

    assert message["type"] == MessageType.EVENT
    assert message["organization_id"] == str(org.id)
    assert message["data"]["is_inference"] is True
    assert message["data"]["event_type"] == "POSSIBLE_INVENTORY_DISCREPANCY"


def test_alert_message_shape(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    event = f.make_event(
        db_session,
        camera,
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        severity=EventSeverity.HIGH,
    )
    alert = f.make_alert(db_session, event)
    db_session.refresh(alert)

    message = alert_message(alert)
    assert message["type"] == MessageType.ALERT
    assert message["data"]["severity"] == "HIGH"
    assert message["store_id"] == str(store.id)


def test_camera_status_message_shape():
    message = camera_status_message(uuid.uuid4(), uuid.uuid4(), None, "ONLINE", measured_fps=9.4)
    assert message["type"] == MessageType.CAMERA_STATUS
    assert message["data"]["status"] == "ONLINE"
    assert message["data"]["measured_fps"] == 9.4


def test_camera_status_message_carries_no_credentials():
    """Camera errors are credential-free by contract; assert it holds."""
    message = camera_status_message(
        uuid.uuid4(),
        uuid.uuid4(),
        None,
        "ERROR",
        error="Unable to connect to camera at 192.168.1.64:554.",
    )
    serialised = json.dumps(message)
    assert "rtsp://" not in serialised
    assert "@" not in message["data"]["error"]


# ---------------------------------------------------------------- end to end
@pytest.fixture()
def ws_world(client, db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    f.make_user(
        db_session,
        org,
        email="ws@duka.co.tz",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.ORGANIZATION_ADMIN,
    )
    db_session.commit()
    tokens = client.post(
        "/api/v1/auth/login", json={"email": "ws@duka.co.tz", "password": PASSWORD}
    ).json()["tokens"]
    return {"org": org, "store": store, "camera": camera, "tokens": tokens}


def test_websocket_rejects_a_missing_token(client, ws_world):
    with client.websocket_connect("/ws/realtime") as socket:
        socket.send_text(json.dumps({"type": "auth"}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_websocket_rejects_a_bad_token(client, ws_world):
    with client.websocket_connect("/ws/realtime") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": "not-a-token"}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_websocket_rejects_a_refresh_token(client, ws_world):
    """A refresh token must not open a live event stream."""
    with client.websocket_connect("/ws/realtime") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": ws_world["tokens"]["refresh_token"]}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_websocket_accepts_a_valid_token(client, ws_world):
    with client.websocket_connect("/ws/realtime") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": ws_world["tokens"]["access_token"]}))
        first = json.loads(socket.receive_text())
        assert first["type"] == MessageType.CONNECTED
        assert first["connection_id"]


def test_websocket_responds_to_ping(client, ws_world):
    with client.websocket_connect("/ws/realtime") as socket:
        socket.send_text(json.dumps({"type": "auth", "token": ws_world["tokens"]["access_token"]}))
        socket.receive_text()  # connected
        socket.send_text(json.dumps({"type": MessageType.PING}))
        assert json.loads(socket.receive_text())["type"] == MessageType.PONG


def test_token_is_not_sent_in_the_url(client, ws_world):
    """A token in a query string ends up in access logs and proxy logs."""
    import inspect

    from app.websocket import routes

    source = inspect.getsource(routes.realtime)
    assert "query_params" not in source
    assert "Query(" not in source
