"""Classification catalogue and the four per-level assignment tables (CLASS-01).

One catalogue per instance (no ``schema_id``, D-12): families group labels, and a
label carries the cardinality of its family. Every assignment row denormalises
``family_id``/``family_multi_valued`` and proves the copies with a composite FK
to the label, so a row can never disagree with its label's family. A partial
unique index on ``(target_id, family_id) WHERE NOT family_multi_valued`` makes
PostgreSQL — not Python — enforce "at most one label per single-valued family
and target" (D-03, T-317.1-04). Deleting an assigned label is RESTRICT; deleting
the target cascades its assignments away (D-01, D-15).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ClassificationFamily(Base):
    """A group of labels sharing a cardinality (D-02, D-03).

    ``is_multi_valued`` is part of the composite-FK target ``(id,
    is_multi_valued)``; it is copied onto every label and assignment row so the
    database can reject a row whose copy disagrees with its family.
    """

    __tablename__ = "classification_families"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Higher ``position`` on a label = stricter (D-10 "strictest wins"); the
    # family only declares whether its positions are ranks.
    is_ranked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # D-14: the personal-data label is fixed, so operators may not add labels to
    # that family. Data-driven instead of a hard-coded key check.
    allows_operator_labels: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # Composite-FK target for ``classification_labels``.
        UniqueConstraint(
            "id", "is_multi_valued", name="uq_classification_families_id_multi"
        ),
    )


class ClassificationLabel(Base):
    """A selectable label of a family; ``key`` is the stable template identifier (D-08)."""

    __tablename__ = "classification_labels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Doubles as rank inside a ranked family (higher = stricter, D-10).
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # Proves the denormalised cardinality copy against the family row.
        ForeignKeyConstraint(
            ["family_id", "family_multi_valued"],
            ["classification_families.id", "classification_families.is_multi_valued"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_classification_labels_family",
        ),
        UniqueConstraint(
            "family_id", "key", name="uq_classification_labels_family_key"
        ),
        # Composite-FK target for the four assignment tables.
        UniqueConstraint(
            "id",
            "family_id",
            "family_multi_valued",
            name="uq_classification_labels_id_family",
        ),
    )


class SchemaClassificationLabel(Base):
    """Schema-level assignment. Table only in 1.0.0 — no endpoint, no UI (D-09)."""

    __tablename__ = "schema_classification_labels"

    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schemas.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["label_id", "family_id", "family_multi_valued"],
            [
                "classification_labels.id",
                "classification_labels.family_id",
                "classification_labels.family_multi_valued",
            ],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_schema_classification_labels_label",
        ),
        Index(
            "uq_schema_classification_labels_single",
            "schema_id",
            "family_id",
            unique=True,
            postgresql_where=text("NOT family_multi_valued"),
        ),
        Index("ix_schema_classification_labels_label_id", "label_id"),
    )


class ObjectTypeClassificationLabel(Base):
    """ObjectType-level assignment. Table only in 1.0.0 — no endpoint, no UI (D-09)."""

    __tablename__ = "object_type_classification_labels"

    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_types.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["label_id", "family_id", "family_multi_valued"],
            [
                "classification_labels.id",
                "classification_labels.family_id",
                "classification_labels.family_multi_valued",
            ],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_object_type_classification_labels_label",
        ),
        Index(
            "uq_object_type_classification_labels_single",
            "object_type_id",
            "family_id",
            unique=True,
            postgresql_where=text("NOT family_multi_valued"),
        ),
        Index("ix_object_type_classification_labels_label_id", "label_id"),
    )


class ObjectTypeAttributeClassificationLabel(Base):
    """Attribute-level assignment — the level the 1.0.0 API and editor write (CLASS-01..03)."""

    __tablename__ = "object_type_attribute_classification_labels"

    object_type_attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_type_attributes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["label_id", "family_id", "family_multi_valued"],
            [
                "classification_labels.id",
                "classification_labels.family_id",
                "classification_labels.family_multi_valued",
            ],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_object_type_attribute_classification_labels_label",
        ),
        Index(
            "uq_object_type_attribute_classification_labels_single",
            "object_type_attribute_id",
            "family_id",
            unique=True,
            postgresql_where=text("NOT family_multi_valued"),
        ),
        Index("ix_object_type_attribute_classification_labels_label_id", "label_id"),
    )


class ObjectClassificationLabel(Base):
    """Object-level assignment — API only in 1.0.0, no UI (D-09, CLASS-05)."""

    __tablename__ = "object_classification_labels"

    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("objects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_multi_valued: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["label_id", "family_id", "family_multi_valued"],
            [
                "classification_labels.id",
                "classification_labels.family_id",
                "classification_labels.family_multi_valued",
            ],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_object_classification_labels_label",
        ),
        Index(
            "uq_object_classification_labels_single",
            "object_id",
            "family_id",
            unique=True,
            postgresql_where=text("NOT family_multi_valued"),
        ),
        Index("ix_object_classification_labels_label_id", "label_id"),
    )
