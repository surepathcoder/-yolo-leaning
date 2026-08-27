"""Milestone 1 -- permissions and multi-tenant isolation.

Tenant isolation is the property that, if broken, ends the product. These
tests are deliberately paranoid.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import select

from app.core.errors import PermissionDeniedError
from app.core.permissions import (
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    Permission,
    permissions_for,
    role_has,
)
from app.core.tenancy import OrganizationScopeViolation, TenantScope, platform_scope
from app.models import Camera, Event, Store
from app.models.enums import UserRole
from tests import factories as f


# ------------------------------------------------------------- permissions
def test_every_role_has_a_permission_set():
    for role in UserRole:
        assert role in ROLE_PERMISSIONS, f"{role} has no permission set"
        assert role in ROLE_DESCRIPTIONS


def test_super_admin_holds_every_permission():
    assert permissions_for(UserRole.SUPER_ADMIN) == frozenset(Permission)


def test_role_privilege_is_strictly_increasing():
    """Each role must be a superset of the one below it where they nest."""
    assert permissions_for(UserRole.VIEWER) < permissions_for(UserRole.ANALYST)
    assert permissions_for(UserRole.VIEWER) < permissions_for(UserRole.SECURITY_OPERATOR)
    assert permissions_for(UserRole.ANALYST) < permissions_for(UserRole.STORE_MANAGER)
    assert permissions_for(UserRole.SECURITY_OPERATOR) < permissions_for(UserRole.STORE_MANAGER)
    assert permissions_for(UserRole.STORE_MANAGER) < permissions_for(UserRole.ORGANIZATION_ADMIN)
    assert permissions_for(UserRole.ORGANIZATION_ADMIN) < permissions_for(UserRole.SUPER_ADMIN)


def test_only_super_admin_may_operate_cross_tenant():
    for role in UserRole:
        expected = role == UserRole.SUPER_ADMIN
        assert role_has(role, Permission.ORG_MANAGE_ALL) is expected


def test_viewer_cannot_write_anything():
    write_permissions = {p for p in Permission if p.value.split(":")[1] != "read"}
    granted = permissions_for(UserRole.VIEWER)
    # The only non-"read" permission a viewer holds is watching a live view.
    assert granted & write_permissions == {Permission.CAMERA_VIEW_LIVE}


def test_analyst_cannot_view_evidence():
    """Analysing traffic patterns does not require looking at people."""
    granted = permissions_for(UserRole.ANALYST)
    assert Permission.EVIDENCE_VIEW not in granted
    assert Permission.EVIDENCE_EXPORT not in granted
    assert Permission.ANALYTICS_READ in granted


def test_security_operator_can_work_alerts_but_not_reconfigure():
    granted = permissions_for(UserRole.SECURITY_OPERATOR)
    assert Permission.ALERT_ACKNOWLEDGE in granted
    assert Permission.ALERT_RESOLVE in granted
    assert Permission.EVIDENCE_VIEW in granted
    assert Permission.CAMERA_WRITE not in granted
    assert Permission.ZONE_WRITE not in granted
    assert Permission.USER_WRITE not in granted


def test_store_manager_cannot_read_the_audit_log():
    assert Permission.AUDIT_READ not in permissions_for(UserRole.STORE_MANAGER)
    assert Permission.AUDIT_READ in permissions_for(UserRole.ORGANIZATION_ADMIN)


def test_every_permission_has_a_description():
    from app.core.permissions import PERMISSION_DESCRIPTIONS

    missing = [p for p in Permission if p not in PERMISSION_DESCRIPTIONS]
    assert missing == []


def test_permission_codes_are_resource_action_pairs():
    for permission in Permission:
        resource, _, action = permission.value.partition(":")
        assert resource and action, permission


# ------------------------------------------------------------- tenant scope
def _scope(org_id, role=UserRole.STORE_MANAGER, stores=()):
    return TenantScope(
        user_id=uuid.uuid4(),
        organization_id=org_id,
        role=role,
        store_ids=frozenset(stores),
    )


def test_require_permission_passes_and_fails(db_session):
    scope = _scope(uuid.uuid4(), UserRole.VIEWER)
    scope.require(Permission.EVENT_READ)
    with pytest.raises(PermissionDeniedError) as exc:
        scope.require(Permission.CAMERA_WRITE)
    assert exc.value.details["required_permission"] == "camera:write"


def test_permission_denied_does_not_leak_internals(db_session):
    scope = _scope(uuid.uuid4(), UserRole.VIEWER)
    with pytest.raises(PermissionDeniedError) as exc:
        scope.require(Permission.CAMERA_WRITE)
    payload = str(exc.value.to_payload())
    assert str(scope.user_id) not in payload


def test_require_organization_blocks_other_tenants():
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    scope = _scope(org_a)
    scope.require_organization(org_a)
    with pytest.raises(OrganizationScopeViolation):
        scope.require_organization(org_b)


def test_require_organization_blocks_null():
    scope = _scope(uuid.uuid4())
    with pytest.raises(OrganizationScopeViolation):
        scope.require_organization(None)


def test_platform_operator_may_cross_organizations():
    scope = platform_scope(uuid.uuid4())
    scope.require_organization(uuid.uuid4())
    scope.require_organization(None)
    assert scope.is_platform_operator is True


def test_store_restriction():
    store_a, store_b = uuid.uuid4(), uuid.uuid4()
    scope = _scope(uuid.uuid4(), stores=[store_a])
    assert scope.is_store_restricted is True
    scope.require_store(store_a)
    with pytest.raises(OrganizationScopeViolation):
        scope.require_store(store_b)
    assert scope.can_access_store(store_a) is True
    assert scope.can_access_store(store_b) is False


def test_unrestricted_scope_accesses_every_store_in_its_org():
    scope = _scope(uuid.uuid4())
    assert scope.is_store_restricted is False
    scope.require_store(uuid.uuid4())
    assert scope.can_access_store(uuid.uuid4()) is True


def test_scope_is_immutable():
    scope = _scope(uuid.uuid4(), UserRole.VIEWER)
    # A frozen dataclass raises FrozenInstanceError; code further down the
    # stack must not be able to widen its own access.
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.role = UserRole.SUPER_ADMIN  # type: ignore[misc]


def test_extra_permissions_from_a_custom_role_are_additive():
    scope = TenantScope(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role=UserRole.VIEWER,
        extra_permissions=frozenset({Permission.EVIDENCE_VIEW}),
    )
    assert scope.has(Permission.EVIDENCE_VIEW) is True
    assert scope.has(Permission.EVENT_READ) is True
    assert scope.has(Permission.CAMERA_WRITE) is False


# --------------------------------------------------- query-level isolation
def test_apply_filters_by_organization(db_session):
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    f.make_store(db_session, org_a, name="A store")
    f.make_store(db_session, org_b, name="B store")

    scope = _scope(org_a.id)
    stores = db_session.scalars(scope.apply(select(Store), Store)).all()
    assert [s.name for s in stores] == ["A store"]


def test_apply_filters_cameras_across_two_tenants(db_session):
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    store_a = f.make_store(db_session, org_a)
    store_b = f.make_store(db_session, org_b)
    f.make_camera(db_session, store_a, name="A-cam")
    f.make_camera(db_session, store_b, name="B-cam")
    f.make_camera(db_session, store_b, name="B-cam-2")

    cameras = db_session.scalars(_scope(org_b.id).apply(select(Camera), Camera)).all()
    assert {c.name for c in cameras} == {"B-cam", "B-cam-2"}
    assert all(c.organization_id == org_b.id for c in cameras)


def test_apply_also_restricts_by_store(db_session):
    org = f.make_organization(db_session)
    store_1 = f.make_store(db_session, org)
    store_2 = f.make_store(db_session, org)
    f.make_camera(db_session, store_1, name="visible")
    f.make_camera(db_session, store_2, name="hidden")

    scope = _scope(org.id, stores=[store_1.id])
    cameras = db_session.scalars(scope.apply(select(Camera), Camera)).all()
    assert [c.name for c in cameras] == ["visible"]


def test_apply_lets_the_platform_operator_see_everything(db_session):
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    f.make_store(db_session, org_a)
    f.make_store(db_session, org_b)

    scope = platform_scope(uuid.uuid4())
    stores = db_session.scalars(scope.apply(select(Store), Store)).all()
    assert len(stores) >= 2


def test_apply_fails_closed_when_a_tenant_user_has_no_organization(db_session):
    scope = TenantScope(user_id=uuid.uuid4(), organization_id=None, role=UserRole.VIEWER)
    with pytest.raises(OrganizationScopeViolation):
        scope.apply(select(Store), Store)


def test_events_are_isolated_between_tenants(db_session):
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    cam_a = f.make_camera(db_session, f.make_store(db_session, org_a))
    cam_b = f.make_camera(db_session, f.make_store(db_session, org_b))
    f.make_event(db_session, cam_a, summary="A event")
    f.make_event(db_session, cam_b, summary="B event")
    f.make_event(db_session, cam_b, summary="B event 2")

    a_events = db_session.scalars(_scope(org_a.id).apply(select(Event), Event)).all()
    b_events = db_session.scalars(_scope(org_b.id).apply(select(Event), Event)).all()
    assert [e.summary for e in a_events] == ["A event"]
    assert len(b_events) == 2


def test_organization_filter_is_applied_before_other_predicates(db_session):
    """The tenant predicate must survive additional filtering."""
    org_a = f.make_organization(db_session)
    org_b = f.make_organization(db_session)
    cam_a = f.make_camera(db_session, f.make_store(db_session, org_a))
    cam_b = f.make_camera(db_session, f.make_store(db_session, org_b))
    f.make_event(db_session, cam_a, summary="shared summary")
    f.make_event(db_session, cam_b, summary="shared summary")

    scope = _scope(org_a.id)
    statement = scope.apply(select(Event), Event).where(Event.summary == "shared summary")
    results = db_session.scalars(statement).all()
    assert len(results) == 1
    assert results[0].organization_id == org_a.id
