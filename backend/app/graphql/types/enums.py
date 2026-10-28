import strawberry
from enum import Enum


@strawberry.enum
class AttributeTypeDataType(Enum):
    DOUBLE = "DOUBLE"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    TEXT = "TEXT"
    DATETIME = "DATETIME"
    JSON = "JSON"
    STATUS = "STATUS"
    VERSION = "VERSION"
    GROUP = "GROUP"
    USER = "USER"
    JIRA = "JIRA"
    CONFLUENCE = "CONFLUENCE"
    REFERENCE = "REFERENCE"
    SELECT = "SELECT"
    URL = "URL"
    EMAIL = "EMAIL"
    TEXTAREA = "TEXTAREA"


@strawberry.enum
class StatusCategory(Enum):
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    INACTIVE = "INACTIVE"
