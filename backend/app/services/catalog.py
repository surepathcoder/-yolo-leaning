"""Seeding of the roles and permissions catalogue.

The catalogue in :mod:`app.core.permissions` is authoritative.  These tables
mirror it so an admin UI can list and describe permissions and so future
per-tenant custom roles can reference them.  Syncing is idempotent: it may be
run on every deployment.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    Permission,
    permission_category,
    permissions_for,
)
from app.models import Permission as PermissionModel
from app.models import Role
from app.models.enums import UserRole

log = get_logger(__name__)


def sync_permission_catalog(session: Session) -> dict[str, int]:
    """Write the code catalogue into the database. Idempotent.

    Permissions removed from the code are *not* deleted: a role in a customer's
    database might still reference one, and silently dropping it would change
    that role's meaning without an audit trail.  They are left in place for a
    deliberate migration to remove.
    """
    existing = {p.code: p for p in session.scalars(select(PermissionModel)).all()}
    created = updated = 0

    for permission in Permission:
        description = PERMISSION_DESCRIPTIONS.get(permission, "")
        category = permission_category(permission)
        row = existing.get(permission.value)
        if row is None:
            session.add(
                PermissionModel(code=permission.value, category=category, description=description)
            )
            created += 1
        elif row.description != description or row.category != category:
            row.description = description
            row.category = category
            updated += 1

    session.flush()
    log.info("permission_catalog_synced", created=created, updated=updated)
    return {"created": created, "updated": updated}


def sync_system_roles(session: Session) -> dict[str, int]:
    """Create or update the six system roles and their permission sets."""
    sync_permission_catalog(session)

    permission_rows = {p.code: p for p in session.scalars(select(PermissionModel)).all()}
    existing = {
        r.name: r
        for r in session.scalars(
            select(Role).where(Role.is_system.is_(True), Role.organization_id.is_(None))
        ).all()
    }

    created = updated = 0
    for role in UserRole:
        desired = {p.value for p in permissions_for(role)}
        row = existing.get(role.value)
        if row is None:
            row = Role(
                name=role.value,
                description=ROLE_DESCRIPTIONS.get(role, ""),
                is_system=True,
                organization_id=None,
            )
            session.add(row)
            session.flush()
            created += 1
        else:
            row.description = ROLE_DESCRIPTIONS.get(role, "")

        current = {p.code for p in row.permissions}
        if current != desired:
            row.permissions = [permission_rows[code] for code in sorted(desired)]
            updated += 1

    session.flush()
    log.info("system_roles_synced", created=created, updated=updated)
    return {"created": created, "updated": updated}
