"""Milestone 1 -- roles/permissions catalogue sync."""

from __future__ import annotations

from sqlalchemy import select

from app.core.permissions import Permission, permissions_for
from app.models import Permission as PermissionModel
from app.models import Role
from app.models.enums import UserRole
from app.services.catalog import sync_permission_catalog, sync_system_roles


def test_permission_catalog_is_written(db_session):
    result = sync_permission_catalog(db_session)
    assert result["created"] == len(Permission)

    codes = set(db_session.scalars(select(PermissionModel.code)).all())
    assert codes == {p.value for p in Permission}


def test_permission_sync_is_idempotent(db_session):
    sync_permission_catalog(db_session)
    second = sync_permission_catalog(db_session)
    assert second == {"created": 0, "updated": 0}
    assert db_session.scalar(select(PermissionModel.code).limit(1)) is not None


def test_permission_descriptions_are_repaired(db_session):
    sync_permission_catalog(db_session)
    row = db_session.scalar(
        select(PermissionModel).where(PermissionModel.code == Permission.CAMERA_READ.value)
    )
    row.description = "wrong"
    db_session.flush()

    result = sync_permission_catalog(db_session)
    assert result["updated"] == 1
    db_session.refresh(row)
    assert row.description != "wrong"


def test_system_roles_are_created_with_their_permissions(db_session):
    sync_system_roles(db_session)

    roles = {r.name: r for r in db_session.scalars(select(Role)).all()}
    assert set(roles) == {r.value for r in UserRole}

    for user_role in UserRole:
        row = roles[user_role.value]
        assert row.is_system is True
        assert row.organization_id is None
        assert {p.code for p in row.permissions} == {p.value for p in permissions_for(user_role)}


def test_role_sync_is_idempotent(db_session):
    sync_system_roles(db_session)
    second = sync_system_roles(db_session)
    assert second == {"created": 0, "updated": 0}


def test_role_permissions_are_repaired_when_drifted(db_session):
    sync_system_roles(db_session)
    viewer = db_session.scalar(select(Role).where(Role.name == UserRole.VIEWER.value))
    viewer.permissions = []
    db_session.flush()

    sync_system_roles(db_session)
    db_session.refresh(viewer)
    assert {p.code for p in viewer.permissions} == {
        p.value for p in permissions_for(UserRole.VIEWER)
    }


def test_super_admin_role_holds_every_permission(db_session):
    sync_system_roles(db_session)
    row = db_session.scalar(select(Role).where(Role.name == UserRole.SUPER_ADMIN.value))
    assert len(row.permissions) == len(Permission)
