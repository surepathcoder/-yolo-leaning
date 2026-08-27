"""Zone and counting-line management.

Geometry validation happens here, before anything is stored, using the same
functions the vision engine uses at runtime. That matters: a zone that passes
the API but fails in the engine produces a camera that silently never fires
its rules, and the installer has no way to tell.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Camera, CountingLine, Zone
from app.models.enums import AuditAction, ZoneType
from app.services.audit import record_audit
from app.services.cameras import get_camera
from vision.zones import GeometryError, normalise_line, normalise_polygon, polygon_area

log = get_logger(__name__)


class InvalidGeometryError(AppError):
    status_code = 422
    code = ErrorCode.INVALID_POLYGON
    message = "The drawn shape is not usable."


def validate_polygon(polygon: list) -> list[list[float]]:
    """Validate a polygon with the engine's own rules.

    Also rejects shapes outside the frame and absurdly small ones: a zone of
    0.1% of the frame will never contain a person's feet reliably, and the
    installer would see "no events" with no explanation.
    """
    try:
        points = normalise_polygon(polygon)
    except GeometryError as exc:
        raise InvalidGeometryError(str(exc)) from exc

    for x, y in points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InvalidGeometryError(
                "Zone points must be inside the camera view (0 to 1).",
                details={"point": [x, y]},
            )

    area = polygon_area(points)
    if area < 0.001:
        raise InvalidGeometryError(
            "This zone is too small to detect anything reliably. "
            "Draw it around the area you want to monitor.",
            details={"area_fraction": round(area, 5)},
        )
    return [[x, y] for x, y in points]


def validate_line(point_a: list, point_b: list) -> tuple[list[float], list[float]]:
    try:
        a, b = normalise_line(point_a, point_b)
    except GeometryError as exc:
        raise InvalidGeometryError(str(exc), code=ErrorCode.INVALID_LINE) from exc

    for x, y in (a, b):
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InvalidGeometryError(
                "Line points must be inside the camera view (0 to 1).",
                code=ErrorCode.INVALID_LINE,
            )

    length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    if length < 0.05:
        raise InvalidGeometryError(
            "This line is too short to count crossings reliably. "
            "Draw it across the full width of the doorway.",
            code=ErrorCode.INVALID_LINE,
            details={"length": round(length, 4)},
        )
    return [a[0], a[1]], [b[0], b[1]]


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


def list_zones(
    session: Session,
    scope: TenantScope,
    *,
    camera_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
) -> list[Zone]:
    scope.require(Permission.ZONE_READ)
    statement = scope.apply(select(Zone).where(Zone.deleted_at.is_(None)), Zone).order_by(
        Zone.created_at
    )
    if camera_id is not None:
        statement = statement.where(Zone.camera_id == camera_id)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(Zone.store_id == store_id)
    return list(session.scalars(statement).all())


def get_zone(session: Session, scope: TenantScope, zone_id: uuid.UUID) -> Zone:
    scope.require(Permission.ZONE_READ)
    zone = session.scalar(
        scope.apply(select(Zone).where(Zone.deleted_at.is_(None)), Zone).where(Zone.id == zone_id)
    )
    if zone is None:
        raise NotFoundError("Zone not found.", details={"zone_id": str(zone_id)})
    return zone


def create_zone(
    session: Session,
    scope: TenantScope,
    *,
    camera_id: uuid.UUID,
    name: str,
    polygon: list,
    zone_type: ZoneType = ZoneType.CUSTOM,
    **fields,
) -> Zone:
    scope.require(Permission.ZONE_WRITE)
    camera: Camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    zone = Zone(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        name=name.strip(),
        zone_type=zone_type,
        polygon=validate_polygon(polygon),
        **fields,
    )
    session.add(zone)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=zone.organization_id,
        user=None,
        resource_type="zone",
        resource_id=str(zone.id),
        description=f"Zone '{zone.name}' ({zone.zone_type.value}) drawn on '{camera.name}'",
    )
    log.info(
        "zone_created",
        zone_id=str(zone.id),
        camera_id=str(camera.id),
        zone_type=zone.zone_type.value,
        points=len(zone.polygon),
    )
    _notify_pipeline(session, camera.id)
    return zone


def update_zone(session: Session, scope: TenantScope, zone_id: uuid.UUID, **changes) -> Zone:
    scope.require(Permission.ZONE_WRITE)
    zone = get_zone(session, scope, zone_id)
    scope.require_store(zone.store_id)

    if "polygon" in changes and changes["polygon"] is not None:
        changes["polygon"] = validate_polygon(changes["polygon"])

    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(zone, field):
            continue
        if getattr(zone, field) != value:
            applied[field] = value
            setattr(zone, field, value)

    if applied:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=zone.organization_id,
            user=None,
            resource_type="zone",
            resource_id=str(zone.id),
            description=f"Zone '{zone.name}' updated",
            changes={k: v for k, v in applied.items() if k != "polygon"},
        )
        _notify_pipeline(session, zone.camera_id)
    return zone


def delete_zone(session: Session, scope: TenantScope, zone_id: uuid.UUID) -> None:
    scope.require(Permission.ZONE_DELETE)
    zone = get_zone(session, scope, zone_id)
    scope.require_store(zone.store_id)

    zone.deleted_at = datetime.now(UTC)
    zone.is_active = False
    session.flush()

    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=zone.organization_id,
        user=None,
        resource_type="zone",
        resource_id=str(zone.id),
        description=f"Zone '{zone.name}' deleted",
    )
    _notify_pipeline(session, zone.camera_id)


# ---------------------------------------------------------------------------
# Counting lines
# ---------------------------------------------------------------------------


def list_lines(
    session: Session,
    scope: TenantScope,
    *,
    camera_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
) -> list[CountingLine]:
    scope.require(Permission.ZONE_READ)
    statement = scope.apply(
        select(CountingLine).where(CountingLine.deleted_at.is_(None)), CountingLine
    ).order_by(CountingLine.created_at)
    if camera_id is not None:
        statement = statement.where(CountingLine.camera_id == camera_id)
    if store_id is not None:
        scope.require_store(store_id)
        statement = statement.where(CountingLine.store_id == store_id)
    return list(session.scalars(statement).all())


def get_line(session: Session, scope: TenantScope, line_id: uuid.UUID) -> CountingLine:
    scope.require(Permission.ZONE_READ)
    line = session.scalar(
        scope.apply(
            select(CountingLine).where(CountingLine.deleted_at.is_(None)), CountingLine
        ).where(CountingLine.id == line_id)
    )
    if line is None:
        raise NotFoundError("Counting line not found.", details={"line_id": str(line_id)})
    return line


def create_line(
    session: Session,
    scope: TenantScope,
    *,
    camera_id: uuid.UUID,
    name: str,
    point_a: list,
    point_b: list,
    **fields,
) -> CountingLine:
    scope.require(Permission.ZONE_WRITE)
    camera: Camera = get_camera(session, scope, camera_id)
    scope.require_store(camera.store_id)

    a, b = validate_line(point_a, point_b)
    line = CountingLine(
        organization_id=camera.organization_id,
        store_id=camera.store_id,
        camera_id=camera.id,
        name=name.strip(),
        point_a=a,
        point_b=b,
        **fields,
    )
    session.add(line)
    session.flush()

    record_audit(
        session,
        action=AuditAction.CREATE,
        organization_id=line.organization_id,
        user=None,
        resource_type="counting_line",
        resource_id=str(line.id),
        description=f"Counting line '{line.name}' drawn on '{camera.name}'",
    )
    _notify_pipeline(session, camera.id)
    return line


def update_line(
    session: Session, scope: TenantScope, line_id: uuid.UUID, **changes
) -> CountingLine:
    scope.require(Permission.ZONE_WRITE)
    line = get_line(session, scope, line_id)
    scope.require_store(line.store_id)

    if changes.get("point_a") is not None or changes.get("point_b") is not None:
        a, b = validate_line(
            changes.get("point_a") or line.point_a,
            changes.get("point_b") or line.point_b,
        )
        changes["point_a"], changes["point_b"] = a, b

    applied: dict[str, object] = {}
    for field, value in changes.items():
        if value is None or not hasattr(line, field):
            continue
        if getattr(line, field) != value:
            applied[field] = value
            setattr(line, field, value)

    if applied:
        session.flush()
        record_audit(
            session,
            action=AuditAction.UPDATE,
            organization_id=line.organization_id,
            user=None,
            resource_type="counting_line",
            resource_id=str(line.id),
            description=f"Counting line '{line.name}' updated",
        )
        _notify_pipeline(session, line.camera_id)
    return line


def delete_line(session: Session, scope: TenantScope, line_id: uuid.UUID) -> None:
    scope.require(Permission.ZONE_DELETE)
    line = get_line(session, scope, line_id)
    scope.require_store(line.store_id)

    line.deleted_at = datetime.now(UTC)
    line.is_active = False
    session.flush()

    record_audit(
        session,
        action=AuditAction.DELETE,
        organization_id=line.organization_id,
        user=None,
        resource_type="counting_line",
        resource_id=str(line.id),
        description=f"Counting line '{line.name}' deleted",
    )
    _notify_pipeline(session, line.camera_id)


# ---------------------------------------------------------------------------
# Live reconfiguration
# ---------------------------------------------------------------------------


def _notify_pipeline(session: Session, camera_id: uuid.UUID) -> None:
    """Push new geometry to a running pipeline without restarting it.

    An operator redrawing a zone should not cost a gap in the video, and a
    restart would reset every track id mid-shift -- which would in turn end
    every dwell timer and re-count everyone at the door.
    """
    try:
        from app.services.pipeline_manager import build_pipeline_config, manager

        running = manager.get(camera_id)
        if running is None:
            return
        camera = session.get(Camera, camera_id)
        if camera is None:
            return
        running.pipeline.apply_config(build_pipeline_config(session, camera))
        log.info("pipeline_reconfigured", camera_id=str(camera_id))
    except Exception as exc:
        # The stored configuration is the truth; a running pipeline that
        # missed an update picks it up on its next start.
        log.warning(
            "pipeline_reconfigure_failed",
            camera_id=str(camera_id),
            error=str(exc)[:200],
        )


def zone_counts(session: Session, scope: TenantScope) -> dict[str, int]:
    """Zone counts by type, for the settings overview."""
    scope.require(Permission.ZONE_READ)
    statement = scope.apply(
        select(Zone.zone_type, func.count()).where(Zone.deleted_at.is_(None)), Zone
    ).group_by(Zone.zone_type)
    counts = {zone_type.value: 0 for zone_type in ZoneType}
    for zone_type, count in session.execute(statement):
        counts[zone_type.value] = int(count)
    return counts
