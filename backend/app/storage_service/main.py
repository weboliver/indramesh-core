from fastapi import FastAPI

from app.logging import configure_logging
from app.storage_service import routes

# Process storage joins the shared configuration before the app is built
# (orchestrator resolution 1). It is an internal-only service, so it gets no
# RequestContextMiddleware and emits no access records.
configure_logging("storage")

app = FastAPI(title="Storage Service")
app.include_router(routes.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}