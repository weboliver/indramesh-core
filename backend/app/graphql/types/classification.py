"""GraphQL type for one assigned classification label (D-07, CLASS-02/CLASS-04).

The same element shape every other surface returns (``ClassificationLabelRef``):
``labelId`` is the catalogue label's id, ``family`` its family key, ``position``
its order/rank inside the family. Read-only: the GraphQL schema has no mutation,
so labels are written through REST/SDK only (D-07).

This module imports scalars only and never a resolver module. It still must not
be imported at module scope by ``resolvers/field_resolvers.py``: the import goes
through the ``app.graphql.types`` package ``__init__``, which eagerly imports
``object_type`` → ``schema`` → ``field_resolvers`` and would close the
type → resolvers → types cycle. ``field_resolvers.py`` therefore uses the same
lazy-import pattern as ``resolvers/mappers.py``.
"""

import strawberry

from app.graphql.types.scalars import UUID


@strawberry.type
class ClassificationLabel:
    """One label assigned to an attribute or an object (D-06: absence = no label)."""

    label_id: UUID
    key: str
    family: str
    name: str
    position: int


def classification_label_from_ref(ref: dict) -> ClassificationLabel:
    """Map one ``classification_service.load_classifications`` ref dict to the type."""
    return ClassificationLabel(
        label_id=ref["label_id"],
        key=ref["key"],
        family=ref["family"],
        name=ref["name"],
        position=ref["position"],
    )


__all__ = ["ClassificationLabel", "classification_label_from_ref"]
