from fastapi import APIRouter, Depends
from app.schemas.auth import UserResponse
from app.services.auth_service import get_public_api_consumer
from app.public_api.attributes import objecttype_attributes_router, schema_attributes_router
from app.public_api.aql import aql_router
from app.public_api.icons import icons_router
from app.public_api.objects import objects_router
from app.public_api.object_types import object_types_router
from app.public_api.object_write import object_write_router
from app.public_api.saved_queries import saved_queries_router
from app.public_api.schemas import schemas_router
from app.public_api.statuses import statuses_router
from app.public_api.graphql import enforce_public_graphql_rate_limit, graphql_router

# D-13 — public realm router. Phase 200 ships only the root route as the
# realm-separation test surface; data endpoints arrive in Phases 201+.
public_router = APIRouter(prefix="/public/api/v1", tags=["public"])
public_router.include_router(objects_router)
public_router.include_router(object_types_router)
public_router.include_router(schemas_router)
public_router.include_router(objecttype_attributes_router)
public_router.include_router(schema_attributes_router)
public_router.include_router(statuses_router)
public_router.include_router(icons_router)
public_router.include_router(object_write_router)
public_router.include_router(aql_router)
public_router.include_router(saved_queries_router)
# D-01 — GraphQLRouter registers its routes at "/" internally (no built-in
# prefix, unlike the REST sub-routers): the prefix must be supplied here.
# Full path = /public/api/v1 + /graphql = /public/api/v1/graphql.
# T-205-07/T-205-09 — rate-limit enforcement attaches HERE via
# dependencies=[...] (runs before Strawberry, scoped to the public router
# only): the internal /api/v1/graphql mount in main.py has no such
# dependency and stays unlimited. Per RESEARCH.md, the public router
# inherits SlowAPI's exemption on Strawberry's shared handlers, so this
# dependency is the ONLY enforcement on the public GraphQL path.
public_router.include_router(
    graphql_router,
    prefix="/graphql",
    dependencies=[Depends(enforce_public_graphql_rate_limit)],
)


@public_router.get("/")
async def public_root(
    current_user: UserResponse = Depends(get_public_api_consumer),
) -> dict:
    # D-16/D-17 — no per-route rate-limit decorator: the global
    # SlowAPIASGIMiddleware (main.py) enforces the identical per-token
    # bucket (same key_func=get_rate_limit_key → public:<sha256>, AUTH-06;
    # its default is pinned to the public limit value by D-17). Each public
    # request pays ONE Redis rate-limit round trip.
    return {"message": "Welcome to Public API", "authenticated_as": current_user.username}
