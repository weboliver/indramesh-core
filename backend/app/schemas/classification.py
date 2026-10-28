"""Classification response and input shapes shared by every surface (CLASS-01, D-07).

A response carries the generic ``classification_labels`` list and derives the
three convenience fields from it, so a cached ``model_dump()`` and a re-validated
cached dict always agree with the generic list. Input and computed fields live in
separate mixins because the response and create/update models deliberately do not
share a base (D-07, RESEARCH Pitfall 10).
"""

import uuid
from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.validators import EscapedStr

FAMILY_CONFIDENTIALITY = "confidentiality"
FAMILY_PERSONAL_DATA = "personal_data"
FAMILY_EXPORT_CONTROL = "export_control"

# The three convenience fields a create/update payload may carry. The generic
# ``classification_labels`` list is read-only (A4).
CLASSIFICATION_INPUT_FIELDS = frozenset({"confidentiality", "personal_data", "export_control"})


class ClassificationLabelRef(BaseModel):
    """One assigned label, as every read producer returns it (D-07).

    ``name`` is the base (English) name; only the catalogue endpoint localises.
    """

    label_id: uuid.UUID
    key: str = Field(max_length=100)
    family: str = Field(max_length=50)
    name: str = Field(max_length=255)
    position: int = 0

    model_config = ConfigDict(from_attributes=True)


class ClassifiedResponseMixin(BaseModel):
    """Generic label list plus the three derived convenience fields (D-06/D-07)."""

    classification_labels: list[ClassificationLabelRef] = Field(default_factory=list)

    @computed_field
    @property
    def confidentiality(self) -> Optional[ClassificationLabelRef]:
        return next(
            (label for label in self.classification_labels if label.family == FAMILY_CONFIDENTIALITY),
            None,
        )

    @computed_field
    @property
    def personal_data(self) -> bool:
        return any(label.family == FAMILY_PERSONAL_DATA for label in self.classification_labels)

    @computed_field
    @property
    def export_control(self) -> list[ClassificationLabelRef]:
        return [
            label for label in self.classification_labels if label.family == FAMILY_EXPORT_CONTROL
        ]


class ClassificationLabelResponse(BaseModel):
    """One catalogue label as the catalogue endpoints return it (D-12, D-13).

    ``family`` is the family key (never a UUID); ``resolved_language`` is the
    language code that supplied ``name``, or ``None`` when the base name was
    served (no translation for the requested language).
    """

    id: uuid.UUID
    family: str = Field(max_length=50)
    key: str = Field(max_length=100)
    name: str = Field(max_length=255)
    position: int = 0
    is_system: bool = False
    resolved_language: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClassificationFamilyResponse(BaseModel):
    """A label family and its labels, both ordered for display (D-02, D-12).

    ``labels`` is always present — an empty list means an operator deleted
    every non-system label of a family, not "unknown".
    """

    id: uuid.UUID
    key: str = Field(max_length=50)
    name: str = Field(max_length=255)
    is_multi_valued: bool
    is_ranked: bool
    is_system: bool
    allows_operator_labels: bool
    sort: int
    labels: list[ClassificationLabelResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ClassificationLabelCreate(BaseModel):
    """Create one operator label in an operator-maintainable family (D-14).

    ``key`` is the stable identifier templates reference (D-08), so it is
    restricted to lowercase snake case and never derived from the name.
    """

    family: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    key: str = Field(pattern=r"^[a-z0-9_]{1,100}$")
    name: EscapedStr = Field(min_length=1, max_length=255)
    position: Optional[int] = None


class ClassificationLabelUpdate(BaseModel):
    """Rename and/or reorder one label; ``key`` and ``family`` are immutable (D-14)."""

    name: Optional[EscapedStr] = Field(default=None, min_length=1, max_length=255)
    position: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class TemplateClassification(BaseModel):
    """The classification block a schema template carries per attribute (D-08).

    Values are the catalogue's stable ``key`` strings, never label ids, so a
    template stored in one instance resolves in another. Only the shape is
    validated here; whether a key exists is decided against the catalogue before
    the first write (D-18). ``extra="forbid"`` makes a malformed block a
    validation error instead of a silently dropped field.
    """

    model_config = ConfigDict(extra="forbid")

    confidentiality: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    personal_data: bool = False
    export_control: list[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,100}$")]] = Field(
        default_factory=list, max_length=50
    )


class ClassificationCreateFields(BaseModel):
    """The three convenience fields accepted on create (D-07, A4)."""

    confidentiality: Optional[uuid.UUID] = None
    personal_data: bool = False
    export_control: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ClassificationUpdateFields(BaseModel):
    """The three convenience fields accepted on update.

    Omitted means unchanged (``model_fields_set``), ``confidentiality: null`` and
    ``export_control: null``/``[]`` clear their family, ``personal_data: null``
    leaves the family unchanged.
    """

    confidentiality: Optional[uuid.UUID] = None
    personal_data: Optional[bool] = None
    export_control: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)
