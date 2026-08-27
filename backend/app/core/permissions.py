"""The authoritative permission catalogue and role mapping.

Permissions live in code, not in the database, because they are part of the
application's contract: adding a permission means adding a check somewhere in
the code, so the two must ship together.  The ``permissions`` and ``roles``
tables mirror this catalogue for the admin UI and for future per-tenant custom
roles, and are seeded from here.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.enums import UserRole


class Permission(StrEnum):
    """A single capability. ``resource:action``."""

    # organizations
    ORG_READ = "org:read"
    ORG_WRITE = "org:write"
    ORG_DELETE = "org:delete"
    ORG_MANAGE_ALL = "org:manage_all"  # cross-tenant; SUPER_ADMIN only

    # users
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    USER_DELETE = "user:delete"

    # stores
    STORE_READ = "store:read"
    STORE_WRITE = "store:write"
    STORE_DELETE = "store:delete"

    # cameras
    CAMERA_READ = "camera:read"
    CAMERA_WRITE = "camera:write"
    CAMERA_DELETE = "camera:delete"
    CAMERA_CONTROL = "camera:control"  # start/stop pipelines, test connection
    CAMERA_VIEW_LIVE = "camera:view_live"

    # zones and lines
    ZONE_READ = "zone:read"
    ZONE_WRITE = "zone:write"
    ZONE_DELETE = "zone:delete"

    # events, alerts, evidence
    EVENT_READ = "event:read"
    EVENT_DELETE = "event:delete"
    ALERT_READ = "alert:read"
    ALERT_ACKNOWLEDGE = "alert:acknowledge"
    ALERT_RESOLVE = "alert:resolve"
    EVIDENCE_VIEW = "evidence:view"
    EVIDENCE_EXPORT = "evidence:export"

    # inventory
    PRODUCT_READ = "product:read"
    PRODUCT_WRITE = "product:write"
    SHELF_READ = "shelf:read"
    SHELF_WRITE = "shelf:write"
    INVENTORY_READ = "inventory:read"

    # pos
    POS_READ = "pos:read"
    POS_WRITE = "pos:write"
    POS_CONFIGURE = "pos:configure"

    # analytics and reports
    ANALYTICS_READ = "analytics:read"
    REPORT_READ = "report:read"
    REPORT_EXPORT = "report:export"

    # models
    MODEL_READ = "model:read"
    MODEL_WRITE = "model:write"
    MODEL_ACTIVATE = "model:activate"

    # system
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    AUDIT_READ = "audit:read"
    SYSTEM_METRICS_READ = "system:metrics_read"


#: Human descriptions, used by the admin UI and the seeded catalogue.
PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.ORG_READ: "View organization details",
    Permission.ORG_WRITE: "Edit organization details",
    Permission.ORG_DELETE: "Delete an organization",
    Permission.ORG_MANAGE_ALL: "Operate across every organization on the platform",
    Permission.USER_READ: "View users",
    Permission.USER_WRITE: "Create and edit users",
    Permission.USER_DELETE: "Deactivate or delete users",
    Permission.STORE_READ: "View stores",
    Permission.STORE_WRITE: "Create and edit stores",
    Permission.STORE_DELETE: "Delete stores",
    Permission.CAMERA_READ: "View camera configuration and status",
    Permission.CAMERA_WRITE: "Add and edit cameras",
    Permission.CAMERA_DELETE: "Remove cameras",
    Permission.CAMERA_CONTROL: "Start, stop and test camera pipelines",
    Permission.CAMERA_VIEW_LIVE: "Watch live camera views",
    Permission.ZONE_READ: "View zones and counting lines",
    Permission.ZONE_WRITE: "Draw and edit zones and counting lines",
    Permission.ZONE_DELETE: "Delete zones and counting lines",
    Permission.EVENT_READ: "View events",
    Permission.EVENT_DELETE: "Delete events",
    Permission.ALERT_READ: "View alerts",
    Permission.ALERT_ACKNOWLEDGE: "Acknowledge alerts",
    Permission.ALERT_RESOLVE: "Resolve and dismiss alerts",
    Permission.EVIDENCE_VIEW: "View event snapshots and clips",
    Permission.EVIDENCE_EXPORT: "Download and export evidence",
    Permission.PRODUCT_READ: "View the product catalogue",
    Permission.PRODUCT_WRITE: "Edit the product catalogue",
    Permission.SHELF_READ: "View shelves",
    Permission.SHELF_WRITE: "Configure shelves",
    Permission.INVENTORY_READ: "View inventory levels and history",
    Permission.POS_READ: "View POS transactions",
    Permission.POS_WRITE: "Import POS transactions",
    Permission.POS_CONFIGURE: "Configure POS providers",
    Permission.ANALYTICS_READ: "View analytics",
    Permission.REPORT_READ: "View reports",
    Permission.REPORT_EXPORT: "Export reports",
    Permission.MODEL_READ: "View detection models",
    Permission.MODEL_WRITE: "Upload and train detection models",
    Permission.MODEL_ACTIVATE: "Activate a model for production use",
    Permission.SETTINGS_READ: "View settings",
    Permission.SETTINGS_WRITE: "Change settings",
    Permission.AUDIT_READ: "Read the audit log",
    Permission.SYSTEM_METRICS_READ: "View system and vision performance metrics",
}


# --------------------------------------------------------------------------
# Role definitions
#
# Built up from least to most privileged so the inheritance is obvious and a
# permission cannot be granted to VIEWER while being forgotten for ANALYST.
# --------------------------------------------------------------------------

#: Read-only access to what is happening now.
_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.ORG_READ,
        Permission.STORE_READ,
        Permission.CAMERA_READ,
        Permission.CAMERA_VIEW_LIVE,
        Permission.ZONE_READ,
        Permission.EVENT_READ,
        Permission.ALERT_READ,
        Permission.PRODUCT_READ,
        Permission.SHELF_READ,
        Permission.INVENTORY_READ,
    }
)

#: Adds historical analysis and reporting. Notably still cannot view evidence
#: images -- analysing traffic patterns does not require looking at people.
_ANALYST: frozenset[Permission] = _VIEWER | {
    Permission.ANALYTICS_READ,
    Permission.REPORT_READ,
    Permission.REPORT_EXPORT,
    Permission.POS_READ,
}

#: The security desk: works the live alert queue and its evidence.
_SECURITY_OPERATOR: frozenset[Permission] = _VIEWER | {
    Permission.ALERT_ACKNOWLEDGE,
    Permission.ALERT_RESOLVE,
    Permission.EVIDENCE_VIEW,
    Permission.CAMERA_CONTROL,
    Permission.ANALYTICS_READ,
}

#: Runs one or more stores end to end.
_STORE_MANAGER: frozenset[Permission] = (
    _ANALYST
    | _SECURITY_OPERATOR
    | {
        Permission.STORE_WRITE,
        Permission.CAMERA_WRITE,
        Permission.ZONE_WRITE,
        Permission.ZONE_DELETE,
        Permission.PRODUCT_WRITE,
        Permission.SHELF_WRITE,
        Permission.POS_WRITE,
        Permission.EVIDENCE_EXPORT,
        Permission.USER_READ,
        Permission.SETTINGS_READ,
        Permission.MODEL_READ,
    }
)

#: Full control of one organization.
_ORGANIZATION_ADMIN: frozenset[Permission] = _STORE_MANAGER | {
    Permission.ORG_WRITE,
    Permission.USER_WRITE,
    Permission.USER_DELETE,
    Permission.STORE_DELETE,
    Permission.CAMERA_DELETE,
    Permission.EVENT_DELETE,
    Permission.POS_CONFIGURE,
    Permission.SETTINGS_WRITE,
    Permission.AUDIT_READ,
    Permission.MODEL_WRITE,
    Permission.MODEL_ACTIVATE,
    Permission.SYSTEM_METRICS_READ,
}

#: Platform operator. The only role that may cross organization boundaries,
#: and every such access is written to the audit log.
_SUPER_ADMIN: frozenset[Permission] = frozenset(Permission)


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.VIEWER: _VIEWER,
    UserRole.ANALYST: _ANALYST,
    UserRole.SECURITY_OPERATOR: _SECURITY_OPERATOR,
    UserRole.STORE_MANAGER: _STORE_MANAGER,
    UserRole.ORGANIZATION_ADMIN: _ORGANIZATION_ADMIN,
    UserRole.SUPER_ADMIN: _SUPER_ADMIN,
}

#: Human descriptions of the roles themselves.
ROLE_DESCRIPTIONS: dict[UserRole, str] = {
    UserRole.SUPER_ADMIN: "Platform operator. Full access across all organizations.",
    UserRole.ORGANIZATION_ADMIN: "Full control of one organization and its stores.",
    UserRole.STORE_MANAGER: "Manages stores, cameras, zones, inventory and staff.",
    UserRole.SECURITY_OPERATOR: "Works the live alert queue and reviews evidence.",
    UserRole.ANALYST: "Reads analytics and reports. No evidence access.",
    UserRole.VIEWER: "Read-only view of current status.",
}


def permissions_for(role: UserRole) -> frozenset[Permission]:
    """Return the permission set granted by a role."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_has(role: UserRole, permission: Permission) -> bool:
    """Whether ``role`` grants ``permission``."""
    return permission in permissions_for(role)


def permission_category(permission: Permission) -> str:
    """The ``resource`` half of a ``resource:action`` code."""
    return permission.value.split(":", 1)[0]
