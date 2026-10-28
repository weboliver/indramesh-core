import strawberry


@strawberry.type
class Health:
    status: str
    timestamp: str
    version: str