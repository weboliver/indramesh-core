"""Shared FieldsFilter dependency for ?fields= query parameter support.

Provides:
  - filter_fields() utility to filter serialized response dicts
  - FieldsFilter dependency class with field name validation
  - Pre-built instances for high-volume endpoints (objects, object-values, object-refs)

Usage in a list endpoint:
    from app.api.fields import objects_fields, filter_fields

    @router.get("/", response_model=List[ObjectResponse])
    async def list_endpoint(
        ...,
        fields: list[str] | None = Depends(objects_fields),
        response: Response = None,
    ):
        items, total = await get_items(...)
        response.headers["X-Total-Count"] = str(total)
        if fields:
            filtered = [
                filter_fields(
                    ObjectResponse.model_validate(item).model_dump(mode="json"),
                    fields
                ) for item in items
            ]
            return JSONResponse(content=filtered, headers=dict(response.headers))
        return items
"""

from fastapi import HTTPException, Query, status


def filter_fields(data: dict, fields: list[str] | None) -> dict:
    """Filter a serialized response dict to only include requested fields.

    Args:
        data: Serialized response dict from Pydantic model.
        fields: List of field names to include, or None to return all fields.

    Returns:
        Filtered dict if fields specified, original dict otherwise.
    """
    if fields is None:
        return data
    return {k: v for k, v in data.items() if k in fields}


class FieldsFilter:
    """FastAPI dependency that parses and validates ?fields= query parameter.

    Per D-06: Opt-in per route via Depends(). Per D-09: Returns 400 for
    invalid field names.

    Usage in a route:
        @router.get("/", response_model=List[XxxResponse])
        async def list_endpoint(
            fields: list[str] | None = Depends(fields_filter),
            ...
        ):
            ...
    """

    def __init__(self, allowed_fields: set[str] | None = None):
        """Create a FieldsFilter dependency.

        Args:
            allowed_fields: Set of valid field names. If None, validation is
                skipped (only used for testing the utility itself).
        """
        self._allowed_fields = allowed_fields

    async def __call__(self, fields: str = Query(None, description="Comma-separated list of fields to include (e.g., ?fields=id,name)")) -> list[str] | None:
        if fields is None:
            return None
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        if not field_list:
            return None
        if self._allowed_fields is not None:
            invalid = [f for f in field_list if f not in self._allowed_fields]
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid field(s): {', '.join(invalid)}. Valid fields: {', '.join(sorted(self._allowed_fields))}"
                )
        return field_list


# Pre-built instances for the three target entities.
# Build allowed field sets from Pydantic response model field names.
from app.schemas.crud import ObjectRefResponse, ObjectResponse, ObjectValueResponse  # noqa: E402

objects_fields = FieldsFilter(
    allowed_fields=set(ObjectResponse.model_fields) | set(ObjectResponse.model_computed_fields)
)
object_values_fields = FieldsFilter(allowed_fields=set(ObjectValueResponse.model_fields.keys()))
object_refs_fields = FieldsFilter(allowed_fields=set(ObjectRefResponse.model_fields.keys()))
