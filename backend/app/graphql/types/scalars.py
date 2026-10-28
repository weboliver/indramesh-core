import uuid
from datetime import datetime
from strawberry.scalars import JSON

UUID = uuid.UUID
DateTime = datetime

__all__ = ["UUID", "DateTime", "JSON"]