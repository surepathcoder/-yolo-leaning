"""Evidence artefact delivery.

Snapshots and clips are never public URLs. Each request is authorised
(``evidence:view``, or ``evidence:export`` to download) and written to the
audit log, because "who looked at the footage" is itself something a customer
needs to be able to see -- ``docs/PRIVACY.md``.

The bytes are served from disk, not proxied through the ORM. A missing file is
a 404, not a 500: retention may have removed it, and that is expected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, DbSession, Scope, get_client_ip, get_user_agent
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.models.enums import AuditAction
from app.services import evidence as evidence_service
from app.services.audit import record_audit

router = APIRouter(prefix="/evidence", tags=["evidence"])
log = get_logger(__name__)


def _filename(evidence) -> str:
    ext = evidence.storage_path.rsplit(".", 1)[-1]
    return f"{evidence.kind.value.lower()}-{evidence.id}.{ext}"


def _serve(
    evidence_id: uuid.UUID,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
    request: Request,
    *,
    disposition: str,
    action: AuditAction,
    description: str,
) -> FileResponse:
    evidence = evidence_service.resolve_evidence(session, scope, evidence_id)
    path = evidence_service.evidence_file_path(evidence)
    if path is None:
        log.warning("evidence_file_missing", evidence_id=str(evidence_id))
        raise NotFoundError(
            "The evidence file is no longer available.",
            details={"evidence_id": str(evidence_id)},
        )

    record_audit(
        session,
        action=action,
        organization_id=evidence.organization_id,
        user=user,
        resource_type="evidence",
        resource_id=str(evidence.id),
        description=description,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    session.commit()

    filename = _filename(evidence)
    return FileResponse(
        path,
        media_type=evidence.mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/{evidence_id}", summary="View an evidence artefact")
def get_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
    request: Request,
) -> FileResponse:
    """Stream one snapshot or clip inline. Requires ``evidence:view``; audited."""
    return _serve(
        evidence_id,
        session,
        scope,
        user,
        request,
        disposition="inline",
        action=AuditAction.EVIDENCE_ACCESSED,
        description="Viewed evidence artefact",
    )


@router.get("/{evidence_id}/download", summary="Download an evidence artefact")
def download_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    scope: Scope,
    user: CurrentUser,
    request: Request,
) -> FileResponse:
    """Download one artefact as an attachment. Requires ``evidence:export``; audited."""
    scope.require(Permission.EVIDENCE_EXPORT)
    return _serve(
        evidence_id,
        session,
        scope,
        user,
        request,
        disposition="attachment",
        action=AuditAction.EXPORT,
        description="Downloaded evidence artefact",
    )
