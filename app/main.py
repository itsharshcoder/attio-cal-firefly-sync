"""Application entry point.

Run with:  uvicorn app.main:app --port 8000
"""

from fastapi import FastAPI

from app import db
from app.clients.attio import attio
from app.config import settings
from app.logging_conf import logger
from app.routers import calcom, fireflies

app = FastAPI(title="Cal.com + Fireflies → Attio sync")

app.include_router(calcom.router)
app.include_router(fireflies.router)


@app.on_event("startup")
async def on_startup() -> None:
    db.init_db()
    logger.info("Started. Internal domains: %s", settings.internal_domains_set)
    try:
        await attio.ensure_custom_attributes()
    except Exception as e:  # noqa: BLE001 - never block startup
        logger.warning("Could not ensure Attio custom attributes: %s", e)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {
        "service": "calcom-fireflies-attio-sync",
        "endpoints": ["/health", "/webhooks/cal", "/webhooks/fireflies", "/docs"],
    }
