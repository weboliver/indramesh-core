import asyncio
import logging
from opensearchpy import AsyncOpenSearch
import os

logger = logging.getLogger(__name__)

opensearch_client: AsyncOpenSearch | None = None
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
OPENSEARCH_USERNAME = os.getenv("OPENSEARCH_USERNAME", "admin")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "")


async def init_opensearch() -> AsyncOpenSearch:
    global opensearch_client
    opensearch_client = AsyncOpenSearch(
        hosts=[OPENSEARCH_URL],
        http_auth=(OPENSEARCH_USERNAME, OPENSEARCH_PASSWORD),
        # maxsize == the retired client's connections_per_node=10.
        maxsize=10,
        # D-09: the transport retry count is a client-level setting in
        # opensearch-py (there is no per-call max_retries). 0 keeps the
        # transport from retrying 429/502/503/504 and thereby multiplying a
        # bounded interactive read; ARQ remains the retry layer for indexing
        # work.
        max_retries=0,
    )

    attempts = 20
    delay = 5

    for attempt in range(attempts):
        try:
            result = await opensearch_client.ping()
            if result:
                logger.info("OpenSearch connected after %d attempts", attempt + 1)
                return opensearch_client
            else:
                if attempt < attempts - 1:
                    logger.warning("OpenSearch not ready (attempt %d/%d), retrying in %ds...", attempt + 1, attempts, delay)
                    await asyncio.sleep(delay)
        except Exception as e:
            if attempt < attempts - 1:
                logger.warning("OpenSearch not ready (attempt %d/%d), retrying in %ds... (%s)", attempt + 1, attempts, delay, e)
                await asyncio.sleep(delay)
            else:
                raise RuntimeError(f"OpenSearch not reachable after {attempts} attempts: {e}")

    raise RuntimeError(f"OpenSearch not reachable after {attempts} attempts")


async def close_opensearch():
    global opensearch_client
    if opensearch_client:
        await opensearch_client.close()
        opensearch_client = None


async def get_opensearch() -> AsyncOpenSearch:
    if opensearch_client is None:
        raise RuntimeError("OpenSearch not initialized")
    return opensearch_client
