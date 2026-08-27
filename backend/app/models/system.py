"""Audit logs, notifications, settings and model versions."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import GUID, Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.enums import (
    AuditAction,
    ModelStatus,
    ModelTask,
    NotificationChannel,
    NotificationStatus,
)
from app.models.mixins import enum_column

if TYPE_CHECKING:
    pass


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """An append-only record of security-relevant actions.

    No ``updated_at`` and no soft delete: an audit row that can be edited is
    not an audit row.  Rows are written for authentication, permission
    changes, configuration changes, evidence access and every cross-tenant
    operation.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_org_created", "organization_id", "created_at"),
        Index("ix_audit_actor_created", "user_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    #: Null for actions taken before an organization context exists
    #: (a failed login against an unknown email, for instance).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Null for anonymous or system-initiated actions.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Preserved separately so the trail stays readable after a user is deleted.
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(40), nullable=True)

    action: Mapped[AuditAction] = mapped_column(
        enum_column(AuditAction), nullable=False, index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    #: Field-level diff for updates. Credential fields are redacted before
    #: they ever reach this column.
    changes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.resource_type}>"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One delivery attempt of an alert over one channel.

    A row per channel, not per alert: an alert that goes to dashboard, email
    and SMS produces three rows, so a failure on one channel is visible and
    retriable without re-sending the others.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_status", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=True, index=True
    )

    channel: Mapped[NotificationChannel] = mapped_column(
        enum_column(NotificationChannel), nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus), nullable=False, default=NotificationStatus.PENDING
    )

    subject: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Email address, phone number or webhook URL. Not a credential.
    destination: Mapped[str | None] = mapped_column(String(400), nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: User-safe failure summary. Provider responses may contain tokens and
    #: are logged, not stored here.
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def __repr__(self) -> str:
        return f"<Notification {self.channel} {self.status}>"


class SystemSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A runtime-adjustable setting.

    Distinct from :class:`app.core.config.Settings`, which holds deployment
    configuration that only an operator with shell access should change.  This
    table holds *business* settings a customer can change from the dashboard.

    ``organization_id`` null means a platform-wide default; a row with an
    organization overrides it for that tenant.
    """

    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("organization_id", "key", name="uq_setting_org_key"),)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False, default="json")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Editable by ORGANIZATION_ADMIN, or platform-operator only.
    is_tenant_editable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<SystemSetting {self.key}>"


class ModelVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A versioned detection model.

    Customer-specific models must never be mixed: a model trained on one
    retailer's products would produce nonsense classes for another.  Every
    non-shared model is bound to an organization, and the loader refuses to
    serve a model whose ``organization_id`` does not match the camera's.

    Only a model with ``status = ACTIVE`` and recorded validation metrics may
    be assigned to a camera -- see ``docs/YOLO.md`` and ``training/``.
    """

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", name="uq_model_org_name_version"),
        Index("ix_models_org_status", "organization_id", "status"),
    )

    #: Null for shared pretrained models available to every tenant.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    task: Mapped[ModelTask] = mapped_column(
        enum_column(ModelTask), nullable=False, default=ModelTask.DETECTION
    )
    status: Mapped[ModelStatus] = mapped_column(
        enum_column(ModelStatus), nullable=False, default=ModelStatus.DRAFT, index=True
    )

    #: Path relative to the model store, e.g. "org-a/products/v3/best.pt".
    weights_path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: SHA-256 of the weights file -- detects a swapped or corrupted model.
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Ordered class names, index = class id.
    class_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    image_size: Mapped[int] = mapped_column(Integer, nullable=False, default=640)

    #: Validation metrics: mAP50, mAP50-95, precision, recall, per-class AP.
    #: Required before a model may become ACTIVE.
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    map50: Mapped[float | None] = mapped_column(Float, nullable=True)
    map50_95: Mapped[float | None] = mapped_column(Float, nullable=True)

    dataset_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dataset_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trained_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    training_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ModelVersion {self.name}:{self.version} {self.status}>"


class DailyMetric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Pre-aggregated daily counters per store.

    Analytics queries scan the events table for detail, but dashboard tiles and
    trend charts read from here.  Aggregating once per day keeps the dashboard
    fast as the events table grows into the millions of rows.
    """

    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("store_id", "metric_date", name="uq_daily_metric_store_date"),
        Index("ix_daily_metrics_org_date", "organization_id", "metric_date"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    people_entered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    people_exited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peak_occupancy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peak_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    total_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    security_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loitering_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    restricted_area_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    after_hours_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_severity_alerts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    low_stock_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shelf_empty_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inventory_discrepancies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Fraction of the day cameras were ONLINE, 0..1. Surfaces the single most
    #: common real-world failure: a camera quietly offline for a week.
    camera_uptime_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Hourly buckets and other detail, ``{"hourly_entries": [...]}``.
    breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<DailyMetric {self.metric_date} store={self.store_id}>"
