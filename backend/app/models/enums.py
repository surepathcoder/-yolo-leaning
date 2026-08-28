"""Enumerations shared by the ORM models, schemas and the vision engine.

All enums are stored as ``VARCHAR`` with a check constraint rather than native
PostgreSQL enum types (``native_enum=False``).  Native enums require an
``ALTER TYPE`` migration to add a single value and cannot be used on SQLite,
which the unit-test suite relies on.  The trade-off is a slightly larger column
in exchange for cheap, portable evolution of the vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------- users
class UserRole(StrEnum):
    """Primary system role. Determines the default permission set."""

    SUPER_ADMIN = "SUPER_ADMIN"
    ORGANIZATION_ADMIN = "ORGANIZATION_ADMIN"
    STORE_MANAGER = "STORE_MANAGER"
    SECURITY_OPERATOR = "SECURITY_OPERATOR"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


# ------------------------------------------------------------- subscriptions
class PlanTier(StrEnum):
    TRIAL = "TRIAL"
    STARTER = "STARTER"
    BUSINESS = "BUSINESS"
    ENTERPRISE = "ENTERPRISE"


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ------------------------------------------------------------------- cameras
class CameraStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    CONNECTING = "CONNECTING"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class StreamProfile(StrEnum):
    """NVRs usually expose a high-resolution main stream and a light sub stream.

    VisionGuard runs inference on the sub stream where available -- it is
    cheaper to decode and detection accuracy at 640px is unaffected.
    """

    MAIN = "MAIN"
    SUB = "SUB"
    MOBILE = "MOBILE"


class VideoSourceKind(StrEnum):
    RTSP = "RTSP"
    FILE = "FILE"
    WEBCAM = "WEBCAM"
    HTTP = "HTTP"
    SYNTHETIC = "SYNTHETIC"


# --------------------------------------------------------------------- zones
class ZoneType(StrEnum):
    RESTRICTED = "RESTRICTED"
    ENTRANCE = "ENTRANCE"
    EXIT = "EXIT"
    SHELF = "SHELF"
    CHECKOUT = "CHECKOUT"
    WAREHOUSE = "WAREHOUSE"
    CUSTOM = "CUSTOM"


class ZoneEventType(StrEnum):
    ENTERED = "ENTERED"
    EXITED = "EXITED"


class CrossingDirection(StrEnum):
    IN = "IN"
    OUT = "OUT"


# -------------------------------------------------------------------- events
class EventType(StrEnum):
    """The complete event vocabulary.

    Names describe what was *observed*.  The two inference-level types are
    explicitly hedged (``POSSIBLE_``, ``POTENTIAL_``) because they are
    conclusions drawn from observations, not measurements.
    """

    # presence / detection
    PERSON_DETECTED = "PERSON_DETECTED"
    OBJECT_DETECTED = "OBJECT_DETECTED"
    VEHICLE_DETECTED = "VEHICLE_DETECTED"

    # zones
    PERSON_ENTERED_ZONE = "PERSON_ENTERED_ZONE"
    PERSON_EXITED_ZONE = "PERSON_EXITED_ZONE"
    OBJECT_ENTERED_ZONE = "OBJECT_ENTERED_ZONE"
    OBJECT_EXITED_ZONE = "OBJECT_EXITED_ZONE"
    RESTRICTED_AREA_ENTRY = "RESTRICTED_AREA_ENTRY"

    # counting
    LINE_CROSSED = "LINE_CROSSED"
    OCCUPANCY_THRESHOLD_EXCEEDED = "OCCUPANCY_THRESHOLD_EXCEEDED"

    # behaviour over time
    LOITERING = "LOITERING"
    DWELL_THRESHOLD_EXCEEDED = "DWELL_THRESHOLD_EXCEEDED"
    AFTER_HOURS_ACTIVITY = "AFTER_HOURS_ACTIVITY"

    # inventory
    PRODUCT_REMOVED = "PRODUCT_REMOVED"
    PRODUCT_RETURNED = "PRODUCT_RETURNED"
    LOW_STOCK = "LOW_STOCK"
    SHELF_EMPTY = "SHELF_EMPTY"
    SHELF_CHANGED = "SHELF_CHANGED"

    # inferences -- conclusions, not observations
    POSSIBLE_INVENTORY_DISCREPANCY = "POSSIBLE_INVENTORY_DISCREPANCY"
    POSSIBLE_EXIT_WITHOUT_PAYMENT = "POSSIBLE_EXIT_WITHOUT_PAYMENT"
    POTENTIAL_SECURITY_CONCERN = "POTENTIAL_SECURITY_CONCERN"

    # system
    CAMERA_ONLINE = "CAMERA_ONLINE"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    PIPELINE_ERROR = "PIPELINE_ERROR"


#: Event types that are *inferences* rather than direct observations.
#: The API and UI must present these with hedged language and a visible
#: confidence level, and they always carry supporting observation ids.
INFERENCE_EVENT_TYPES = frozenset(
    {
        EventType.POSSIBLE_INVENTORY_DISCREPANCY,
        EventType.POSSIBLE_EXIT_WITHOUT_PAYMENT,
        EventType.POTENTIAL_SECURITY_CONCERN,
    }
)


class EventSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Ordering used for sorting and threshold comparisons.
SEVERITY_ORDER: dict[EventSeverity, int] = {
    EventSeverity.INFO: 0,
    EventSeverity.LOW: 1,
    EventSeverity.MEDIUM: 2,
    EventSeverity.HIGH: 3,
    EventSeverity.CRITICAL: 4,
}


class ConfidenceLevel(StrEnum):
    """Qualitative confidence shown to users alongside inferences.

    Deliberately coarse: a numeric score implies a precision the system does
    not have when combining heterogeneous signals.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# -------------------------------------------------------------------- alerts
class AlertStatus(StrEnum):
    NEW = "NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class NotificationChannel(StrEnum):
    DASHBOARD = "DASHBOARD"
    WEBSOCKET = "WEBSOCKET"
    EMAIL = "EMAIL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    WEBHOOK = "WEBHOOK"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# ------------------------------------------------------------------ evidence
class EvidenceKind(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    CLIP = "CLIP"
    THUMBNAIL = "THUMBNAIL"
    METADATA = "METADATA"


# ----------------------------------------------------------------- inventory
class OccupancyLevel(StrEnum):
    """Coarse shelf fullness.

    Deliberately not an exact count: CCTV at shelf-monitoring angles cannot
    reliably resolve individual units, and pretending otherwise would produce
    confident wrong numbers.
    """

    FULL = "FULL"
    NORMAL = "NORMAL"
    LOW = "LOW"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


class InventoryEventType(StrEnum):
    PRODUCT_REMOVED = "PRODUCT_REMOVED"
    PRODUCT_RETURNED = "PRODUCT_RETURNED"
    LOW_STOCK = "LOW_STOCK"
    SHELF_EMPTY = "SHELF_EMPTY"
    SHELF_CHANGED = "SHELF_CHANGED"
    SHELF_RESTOCKED = "SHELF_RESTOCKED"


# ----------------------------------------------------------------------- pos
class POSTransactionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    VOIDED = "VOIDED"
    REFUNDED = "REFUNDED"
    PENDING = "PENDING"


class CorrelationStatus(StrEnum):
    """Outcome of matching a camera observation against POS data."""

    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"
    UNCERTAIN = "UNCERTAIN"


# -------------------------------------------------------------------- models
class ModelTask(StrEnum):
    DETECTION = "DETECTION"
    SEGMENTATION = "SEGMENTATION"
    CLASSIFICATION = "CLASSIFICATION"
    POSE = "POSE"


class ModelStatus(StrEnum):
    DRAFT = "DRAFT"
    TRAINING = "TRAINING"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


# --------------------------------------------------------------------- audit
class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    ACCESS_DENIED = "ACCESS_DENIED"
    EVIDENCE_ACCESSED = "EVIDENCE_ACCESSED"
    EXPORT = "EXPORT"
    CONFIG_CHANGED = "CONFIG_CHANGED"
