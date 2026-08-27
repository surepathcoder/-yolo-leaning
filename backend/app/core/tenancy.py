"""Tenant scoping.

Multi-tenant isolation is the single most important correctness property of
this system: one customer seeing another's camera feeds would end the product.
Isolation is therefore enforced through one explicit, testable object rather
than by remembering to add a ``WHERE`` clause in each query.

The rules:

1. Every query for tenant-owned data goes through :func:`scoped` or applies
   :meth:`TenantScope.apply` itself.
2. ``organization_id`` is the *first* predicate, so the tenant index is used.
3. Cross-organization access requires ``ORG_MANAGE_ALL`` and is audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select

from app.core.errors import AppError, ErrorCode, PermissionDeniedError
from app.core.permissions import Permission, permissions_for
from app.models.enums import UserRole


class OrganizationScopeViolation(AppError):
    """Raised when a caller touches data outside their organization."""

    status_code = 403
    code = ErrorCode.ORGANIZATION_SCOPE_VIOLATION
    message = "You do not have access to this resource."


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Everything needed to decide what a caller may see.

    Immutable on purpose: a scope is derived once from the authenticated user
    and passed down.  Code further down the stack cannot widen its own access.
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    role: UserRole
    #: Empty means "every store in the organization"; a non-empty set
    #: restricts the caller to those stores.
    store_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    #: Extra permissions granted by a custom role, on top of the system role.
    extra_permissions: frozenset[Permission] = field(default_factory=frozenset)

    # ------------------------------------------------------------- properties
    @property
    def is_platform_operator(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role) | self.extra_permissions

    @property
    def is_store_restricted(self) -> bool:
        return bool(self.store_ids)

    # ----------------------------------------------------------------- checks
    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Raise unless the caller holds ``permission``."""
        if not self.has(permission):
            raise PermissionDeniedError(
                details={"required_permission": permission.value},
                internal_message=(f"user={self.user_id} role={self.role} lacks {permission.value}"),
            )

    def require_organization(self, organization_id: uuid.UUID | None) -> None:
        """Raise unless the caller may act on ``organization_id``.

        A platform operator may cross organizations; everyone else may not, and
        the attempt is reported as a scope violation rather than a 404 so it
        shows up clearly in the audit log.
        """
        if self.is_platform_operator:
            return
        if organization_id is None or organization_id != self.organization_id:
            raise OrganizationScopeViolation(
                internal_message=(
                    f"user={self.user_id} org={self.organization_id} "
                    f"attempted access to org={organization_id}"
                )
            )

    def require_store(self, store_id: uuid.UUID | None) -> None:
        """Raise unless the caller may act on ``store_id``."""
        if self.is_platform_operator or not self.is_store_restricted:
            return
        if store_id is None or store_id not in self.store_ids:
            raise OrganizationScopeViolation(
                internal_message=(
                    f"user={self.user_id} restricted to {len(self.store_ids)} stores, "
                    f"attempted store={store_id}"
                )
            )

    def can_access_store(self, store_id: uuid.UUID) -> bool:
        if self.is_platform_operator or not self.is_store_restricted:
            return True
        return store_id in self.store_ids

    # ------------------------------------------------------------------ query
    def apply(self, statement: Select, model: type, *, store_column: str = "store_id") -> Select:
        """Add the tenant predicates to ``statement``.

        ``model`` must expose ``organization_id``.  If it also exposes the
        column named by ``store_column`` and the caller is store-restricted,
        that filter is applied too.
        """
        if not self.is_platform_operator:
            if self.organization_id is None:
                # A non-super-admin with no organization can see nothing. This
                # should be unreachable, but failing closed is the right shape.
                raise OrganizationScopeViolation(
                    internal_message=f"user={self.user_id} has no organization"
                )
            statement = statement.where(model.organization_id == self.organization_id)

        if self.is_store_restricted:
            column = getattr(model, store_column, None)
            if column is not None:
                statement = statement.where(column.in_(self.store_ids))
        return statement


def platform_scope(user_id: uuid.UUID) -> TenantScope:
    """A scope with unrestricted access. Only for SUPER_ADMIN and migrations."""
    return TenantScope(user_id=user_id, organization_id=None, role=UserRole.SUPER_ADMIN)
