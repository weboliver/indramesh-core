"""Neutral attribute-type alias catalogue (SEAM-04, D-09).

Pure data mapping the accepted legacy ``type_string`` values onto the canonical
:class:`AttributeTypeDataType`. Extracted from the retired Core AI service (now
the ai-schema-wizard plugin, 306) so the manual atomic/template create path AND
the strict schema-from-tree materializer keep the exact same accepted alias set.

Every entry that existed before the extraction is preserved verbatim — including
``status`` (the Phase-245 ``schema_service`` handling). The strict path rejects
only names absent from this map; known aliases stay accepted (D-09, R05-4).
"""

from app.models.attribute_type import AttributeTypeDataType

TYPE_STRING_MAP: dict[str, AttributeTypeDataType] = {
    "text": AttributeTypeDataType.TEXT,
    "string": AttributeTypeDataType.TEXT,
    "integer": AttributeTypeDataType.INTEGER,
    "int": AttributeTypeDataType.INTEGER,
    "number": AttributeTypeDataType.INTEGER,
    "boolean": AttributeTypeDataType.BOOLEAN,
    "bool": AttributeTypeDataType.BOOLEAN,
    "float": AttributeTypeDataType.DOUBLE,
    "double": AttributeTypeDataType.DOUBLE,
    "decimal": AttributeTypeDataType.DOUBLE,
    "datetime": AttributeTypeDataType.DATETIME,
    "date": AttributeTypeDataType.DATETIME,
    "timestamp": AttributeTypeDataType.DATETIME,
    "url": AttributeTypeDataType.URL,
    "link": AttributeTypeDataType.URL,
    "email": AttributeTypeDataType.EMAIL,
    "mail": AttributeTypeDataType.EMAIL,
    "select": AttributeTypeDataType.SELECT,
    "dropdown": AttributeTypeDataType.SELECT,
    "choice": AttributeTypeDataType.SELECT,
    "reference": AttributeTypeDataType.REFERENCE,
    "relation": AttributeTypeDataType.REFERENCE,
    "foreign key": AttributeTypeDataType.REFERENCE,
    "status": AttributeTypeDataType.STATUS,
}
