"""SQLAlchemy models.

Importing this package registers every model on ``Base.metadata``, which is
what Alembic autogenerate and ``Base.metadata.create_all`` rely on.  Import the
package -- not individual modules -- anywhere the full schema is needed.
"""

from __future__ import annotations

from app.models.camera import Camera, CameraStream
from app.models.detection import DetectionEvent, TrackingObject
from app.models.event import Alert, Event, EventEvidence
from app.models.inventory import (
    InventoryEvent,
    InventorySnapshot,
    Product,
    ProductDetection,
    Shelf,
    shelf_products,
)
from app.models.organization import Organization, Subscription
from app.models.pos import CorrelationResult, POSItem, POSTransaction
from app.models.store import Store, StoreOperatingHours
from app.models.system import (
    AuditLog,
    DailyMetric,
    ModelVersion,
    Notification,
    SystemSetting,
)
from app.models.user import (
    Permission,
    RefreshToken,
    Role,
    User,
    role_permissions,
    user_stores,
)
from app.models.zone import (
    CountingLine,
    LineCrossingEvent,
    Zone,
    ZoneEvent,
)

__all__ = [
    "Alert",
    "AuditLog",
    "Camera",
    "CameraStream",
    "CorrelationResult",
    "CountingLine",
    "DailyMetric",
    "DetectionEvent",
    "Event",
    "EventEvidence",
    "InventoryEvent",
    "InventorySnapshot",
    "LineCrossingEvent",
    "ModelVersion",
    "Notification",
    "Organization",
    "POSItem",
    "POSTransaction",
    "Permission",
    "Product",
    "ProductDetection",
    "RefreshToken",
    "Role",
    "Shelf",
    "Store",
    "StoreOperatingHours",
    "Subscription",
    "SystemSetting",
    "TrackingObject",
    "User",
    "Zone",
    "ZoneEvent",
    "role_permissions",
    "shelf_products",
    "user_stores",
]
