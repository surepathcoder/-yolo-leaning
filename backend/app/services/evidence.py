"""Evidence storage and retrieval.

The vision pipeline hands the backend finished artefacts as plain ``bytes``
(:class:`vision.evidence.EvidenceArtifact`). This module is where they land on
disk and gain an :class:`~app.models.event.EventEvidence` row.

Two rules from ``docs/SECURITY.md`` shape it:

* **Files never live in the database.** A row carries a *relative* path under
  ``EVIDENCE_STORAGE_PATH`` -- never absolute, so storage can move -- plus a
  SHA-256 so tampering is detectable.
* **Every artefact has a death date.** ``expires_at`` is set on creation from
  the retention policy, and :func:`prune_expired_evidence` (run by the
  maintenance sweep) deletes the row *and* the file once it passes.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.tenancy import TenantScope
from app.models import Event, EventEvidence
from app.models.enums import EvidenceKind
from app.services import events as event_service

log = get_logger(__name__)

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4"}
#: Clips are the heavier, more sensitive artefact and follow the clip policy;
#: stills follow the snapshot policy.
_CLIP_KINDS = {EvidenceKind.CLIP}


def _evidence_root() -> Path:
    return Path(get_settings().EVIDENCE_STORAGE_PATH)


def _retention_days(kind: EvidenceKind) -> int:
    settings = get_settings()
    if kind in _CLIP_KINDS:
        return settings.EVIDENCE_RETENTION_DAYS
    return settings.SNAPSHOT_RETENTION_DAYS


def relative_path_for(event: Event, kind: EvidenceKind, mime_type: str) -> str:
    """A stable, collision-free relative path for one artefact.

    Laid out ``<org>/<yyyy>/<mm>/<dd>/<event>/<kind>-<rand>.<ext>`` so a day's
    evidence for one tenant sits together and a retention sweep can reason
    about it by prefix.
    """
    ext = _EXTENSIONS.get(mime_type, "bin")
    stamp = event.occurred_at.astimezone(UTC)
    return (
        f"{event.organization_id}/{stamp:%Y/%m/%d}/{event.id}/"
        f"{kind.value.lower()}-{uuid.uuid4().hex[:8]}.{ext}"
    )


def store_artifact(
    session: Session,
    event: Event,
    *,
    kind: EvidenceKind,
    data: bytes,
    mime_type: str = "image/jpeg",
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    bbox: list | None = None,
    captured_at: datetime | None = None,
) -> EventEvidence:
    """Write bytes to disk and link an ``EventEvidence`` row to ``event``."""
    relative = relative_path_for(event, kind, mime_type)
    destination = _evidence_root() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)

    evidence = event_service.attach_evidence(
        session,
        event,
        kind=kind,
        storage_path=relative,
        mime_type=mime_type,
        size_bytes=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        bbox=bbox,
        captured_at=captured_at,
        retention_days=_retention_days(kind),
    )
    log.info(
        "evidence_stored",
        event_id=str(event.id),
        kind=kind.value,
        bytes=len(data),
        path=relative,
    )
    return evidence


def resolve_evidence(session: Session, scope: TenantScope, evidence_id: uuid.UUID) -> EventEvidence:
    """The evidence row, if the caller may see it.

    ``ANALYST`` deliberately lacks ``evidence:view``: analysing traffic
    patterns does not require looking at pictures of people (``docs/PRIVACY.md``).
    """
    scope.require(Permission.EVIDENCE_VIEW)
    # Scope through the joined Event so a store-restricted operator is held to
    # their stores -- EventEvidence itself has no store_id column.
    statement = scope.apply(
        select(EventEvidence).join(Event, EventEvidence.event_id == Event.id), Event
    )
    evidence = session.scalar(statement.where(EventEvidence.id == evidence_id))
    if evidence is None:
        raise NotFoundError("Evidence not found.", details={"evidence_id": str(evidence_id)})
    return evidence


def evidence_file_path(evidence: EventEvidence) -> Path | None:
    """The absolute path of the artefact on disk, or ``None`` if it is gone.

    The stored path is system-generated, but the containment check is cheap
    insurance against a future path ever coming from user input.
    """
    root = _evidence_root().resolve()
    candidate = (root / evidence.storage_path).resolve()
    if root not in candidate.parents:
        log.error("evidence_path_escape", evidence_id=str(evidence.id), path=evidence.storage_path)
        return None
    return candidate if candidate.is_file() else None


def prune_expired_evidence(
    session: Session, *, now: datetime | None = None, limit: int = 2000
) -> int:
    """Delete evidence rows (and their files) past ``expires_at``. Returns how many.

    Retention is a privacy obligation, not housekeeping -- see ``docs/PRIVACY.md``.
    """
    moment = now or datetime.now(UTC)
    rows = session.scalars(
        select(EventEvidence)
        .where(EventEvidence.expires_at.is_not(None), EventEvidence.expires_at < moment)
        .limit(limit)
    ).all()

    root = _evidence_root()
    for row in rows:
        target = (root / row.storage_path).resolve()
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("evidence_file_unlink_failed", path=row.storage_path, error=str(exc)[:200])
        session.delete(row)

    session.flush()
    if rows:
        log.info("evidence_pruned", deleted=len(rows))
    return len(rows)
