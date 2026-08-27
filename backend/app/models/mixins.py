"""Model helpers shared across the schema."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db.base import GUID


def enum_column(enum_cls: type, **kwargs: Any) -> SAEnum:
    """Portable enum column.

    Stored as ``VARCHAR`` + check constraint so the value set can be extended
    with an ordinary migration and the same models run on SQLite in tests.

    Three settings matter here:

    ``values_callable``
        Persist the enum *value* rather than the member name, keeping the
        database readable.
    ``create_constraint=True``
        SQLAlchemy defaults this to ``False``.  Without it a non-native enum is
        an unconstrained ``VARCHAR`` and a bad write -- from a bug, a manual
        query or a botched migration -- silently corrupts the column.
    ``name``
        Explicit, so the generated constraint is named deterministically and
        migrations stay reviewable.
    """
    kwargs.setdefault("name", enum_cls.__name__.lower())
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=64,
        create_constraint=True,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
        **kwargs,
    )


class OrganizationScopedMixin:
    """Every tenant-owned row carries its organization id.

    Denormalising ``organization_id`` onto leaf tables (events, detections,
    alerts) rather than joining up through store -> camera is deliberate: it
    lets the tenant filter be the *first*, index-covered predicate on every
    query.  That matters most on the events table, which is the largest in the
    database and the one users filter constantly.
    """

    @declared_attr
    @classmethod
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            GUID(),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            sort_order=-90,
        )
