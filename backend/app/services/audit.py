"""Audit logging.

Audit rows are append-only and must never contain a credential. The
``changes`` diff is filtered through :data:`REDACTED_FIELDS` before it is
written -- a defence in depth alongside the logging redactor, because this
data is persisted rather than merely printed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import AuditLog, User
from app.models.enums import AuditAction

log = get_logger(__name__)

#: Fields never written into an audit diff.
REDACTED_FIELDS = frozenset(
    {
        "password",
        "hashed_password",
        "new_password",
        "current_password",
        "encrypted_password",
        "token",
        "access_token",
        "refresh_token",
        "token_hash",
        "jwt_secret",
        "api_key",
        "credentials_encryption_key",
    }
)

REDACTED = "***redacted***"


def sanitize_changes(changes: dict[str, Any] | None) -> dict[str, Any]:
    """Strip secrets out of a change diff, recursively."""
    if not changes:
        return {}
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key.lower() in REDACTED_FIELDS:
            clean[key] = REDACTED
        elif isinstance(value, dict):
            clean[key] = sanitize_changes(value)
        elif isinstance(value, uuid.UUID):
            clean[key] = str(value)
        elif isinstance(value, datetime):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


def record_audit(
    session: Session,
    *,
    action: AuditAction,
    organization_id: uuid.UUID | None,
    user: User | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    description: str = "",
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    succeeded: bool = True,
    actor_email: str | None = None,
) -> AuditLog:
    """Append one audit row.

    The actor's email and role are copied onto the row so the trail stays
    readable after the user is deleted.
    """
    entry = AuditLog(
        organization_id=organization_id,
        user_id=user.id if user else None,
        actor_email=(user.email if user else actor_email),
        actor_role=(user.role.value if user else None),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        description=description[:500],
        changes=sanitize_changes(changes),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
        request_id=request_id,
        succeeded=succeeded,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    session.flush()
    return entry


def record_cross_tenant_access(
    session: Session,
    *,
    user: User,
    target_organization_id: uuid.UUID,
    resource_type: str,
    resource_id: str | None = None,
    description: str = "",
    request_id: str | None = None,
) -> AuditLog:
    """Record a platform operator reaching into a customer's data.

    Cross-tenant access is legitimate for support, but it must never be
    invisible: every such read is logged against the target organization so
    the customer's own audit view shows it.
    """
    log.warning(
        "cross_tenant_access",
        user_id=str(user.id),
        target_organization_id=str(target_organization_id),
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return record_audit(
        session,
        action=AuditAction.EVIDENCE_ACCESSED if resource_type == "evidence" else AuditAction.EXPORT,
        organization_id=target_organization_id,
        user=user,
        resource_type=resource_type,
        resource_id=resource_id,
        description=description or f"Platform operator accessed {resource_type}",
        request_id=request_id,
    )


def list_audit_entries(
    session: Session,
    *,
    organization_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    action: AuditAction | None = None,
    user_id: uuid.UUID | None = None,
) -> list[AuditLog]:
    """Read the audit trail for one organization, newest first."""
    statement = select(AuditLog).where(AuditLog.organization_id == organization_id)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if user_id is not None:
        statement = statement.where(AuditLog.user_id == user_id)
    statement = statement.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(statement).all())
