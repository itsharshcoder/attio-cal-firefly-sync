"""HTTP endpoint that Fireflies POSTs to when a transcript is ready.

Same pattern as Cal.com: verify -> 200 -> background work. De-duplication is
handled downstream in Attio (the meeting id is stored on the Company), so there
is no local database here.
"""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.logging_conf import logger
from app.security import verify_signature
from app.services import fireflies_service

router = APIRouter(prefix="/webhooks/fireflies", tags=["Fireflies"])


@router.get("")
async def fireflies_verify() -> dict:
    """Some webhook UIs send a GET to verify the URL is live. Reply 200."""
    return {"status": "ok", "endpoint": "fireflies-webhook"}


@router.post("")
async def fireflies_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    # Print exactly what Fireflies sent (headers + raw body), before anything else.
    logger.info(
        "=== FIREFLIES WEBHOOK ===\n  x-hub-signature: %s\n  raw body: %s",
        request.headers.get("x-hub-signature", "(none)"),
        raw.decode("utf-8", "replace") or "(empty)",
    )

    # Fireflies signs with HMAC-SHA256 in the `x-hub-signature` header (§6.2).
    signature = request.headers.get("x-hub-signature", "")
    if not verify_signature(raw, signature, settings.fireflies_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Be tolerant of test/verification pings that aren't valid JSON.
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        logger.info("Fireflies sent a non-JSON ping - acknowledging.")
        return {"status": "ok", "note": "ping acknowledged"}

    # Fireflies actually sends {"event": "meeting.transcribed", "meeting_id": ...}.
    # Accept that plus the spec's {"eventType": "Transcription completed", "meetingId": ...}.
    event_type = body.get("event") or body.get("eventType", "")
    meeting_id = body.get("meeting_id") or body.get("meetingId", "")

    TRANSCRIBED_EVENTS = {"meeting.transcribed", "Transcription completed"}
    if event_type not in TRANSCRIBED_EVENTS:
        logger.info("Ignoring Fireflies event: %s", event_type or "(verification ping)")
        return {"status": "ok", "note": "event ignored"}

    logger.info("Received Fireflies 'Transcription completed' for %s", meeting_id)

    async def _safe(mid: str) -> None:
        try:
            await fireflies_service.handle_transcription_completed(mid)
        except Exception as e:  # noqa: BLE001
            logger.error("Fireflies handler failed for %s: %s", mid, e)

    background.add_task(_safe, meeting_id)

    return {"status": "accepted", "meeting_id": meeting_id}
