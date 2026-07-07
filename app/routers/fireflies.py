"""Fireflies webhook endpoint: verify -> de-dupe -> ack 200 -> process in background."""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import db
from app.config import settings
from app.logging_conf import logger
from app.security import verify_signature
from app.services import fireflies_service

router = APIRouter(prefix="/webhooks/fireflies", tags=["Fireflies"])


@router.get("")
async def fireflies_verify() -> dict:
    """Reply 200 to GET-based URL verification checks."""
    return {"status": "ok", "endpoint": "fireflies-webhook"}


@router.post("")
async def fireflies_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    signature = request.headers.get("x-hub-signature", "")
    if not verify_signature(raw, signature, settings.fireflies_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Tolerate test/verification pings that aren't valid JSON.
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"status": "ok", "note": "ping acknowledged"}

    # Fireflies sends {"event": "meeting.transcribed", "meeting_id": ...};
    # also accept the spec's {"eventType": "Transcription completed", "meetingId": ...}.
    event_type = body.get("event") or body.get("eventType", "")
    meeting_id = body.get("meeting_id") or body.get("meetingId", "")

    if event_type not in {"meeting.transcribed", "Transcription completed"}:
        logger.info("Ignoring Fireflies event: %s", event_type or "(verification ping)")
        return {"status": "ok", "note": "event ignored"}

    idem_key = f"fireflies:{event_type}:{meeting_id}"
    if db.already_processed(idem_key):
        logger.info("Duplicate Fireflies webhook %s — ignoring.", idem_key)
        return {"status": "ok", "note": "duplicate ignored"}
    db.store_event("fireflies", idem_key, raw.decode("utf-8", "replace"))

    logger.info("Received Fireflies 'Transcription completed' for %s", meeting_id)

    async def _safe(mid: str) -> None:
        try:
            await fireflies_service.handle_transcription_completed(mid)
        except Exception as e:  # noqa: BLE001
            logger.error("Fireflies handler failed for %s: %s", mid, e)

    background.add_task(_safe, meeting_id)
    return {"status": "accepted", "meeting_id": meeting_id}
