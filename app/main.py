"""Application entry point.

Run it with:   uvicorn app.main:app --reload --port 8000

`app.main:app` means "in the file app/main.py, use the variable named `app`".
`--reload` restarts the server automatically whenever you save a code change.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.clients.attio import attio
from app.config import settings
from app.logging_conf import logger
from app.routers import calcom, fireflies
from app.services.fireflies_service import poll_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook (the modern replacement for @app.on_event)."""
    # Table-less: all state lives in Attio, nothing to initialise locally.
    logger.info("Started (table-less). Internal domains: %s", settings.internal_domains_set)
    logger.info("Attio live: %s | Fireflies live: %s | Analysis live: %s",
                bool(settings.attio_api_key),
                bool(settings.fireflies_api_key),
                bool(settings.anthropic_api_key))
    # Ensure the custom dsv_* attributes exist in Attio (auto-create; required).
    try:
        await attio.ensure_custom_attributes()
    except Exception as e:  # noqa: BLE001 - never block startup on this
        logger.warning("Could not ensure Attio custom attributes: %s", e)

    # Auto-fetch: poll Fireflies for new transcripts in the background (if enabled).
    poll_task = None
    if settings.fireflies_poll_seconds > 0 and settings.fireflies_api_key:
        poll_task = asyncio.create_task(poll_loop())
        logger.info("Fireflies auto-fetch ENABLED (every %ds).", settings.fireflies_poll_seconds)
    else:
        logger.info("Fireflies auto-fetch OFF (webhook-only). Set FIREFLIES_POLL_SECONDS>0 to enable.")

    yield

    if poll_task:
        poll_task.cancel()


app = FastAPI(title="Cal.com + Fireflies → Attio sync", lifespan=lifespan)

# Register the two webhook endpoints.
app.include_router(calcom.router)
app.include_router(fireflies.router)


@app.get("/health")
async def health() -> dict:
    """A simple 'is it alive?' endpoint. Open http://localhost:8000/health."""
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {
        "service": "calcom-fireflies-attio-sync",
        "endpoints": ["/health", "/webhooks/cal", "/webhooks/fireflies", "/docs"],
    }
