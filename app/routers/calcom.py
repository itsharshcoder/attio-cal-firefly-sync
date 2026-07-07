"""Cal.com webhook endpoint: verify -> de-dupe -> ack 200 -> process in background."""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import db
from app.config import settings
from app.logging_conf import logger
from app.models import CalcomBookingPayload
from app.security import verify_signature
from app.services import calcom_service

router = APIRouter(prefix="/webhooks/cal", tags=["Cal.com"])


async def _safe(handler, payload) -> None:
    """Run a background handler, logging a one-line error instead of a traceback."""
    try:
        await handler(payload)
    except Exception as e:  # noqa: BLE001
        logger.error("Cal.com handler %s failed for %s: %s",
                     getattr(handler, "__name__", handler), payload.uid, e)


@router.post("")
async def cal_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    signature = request.headers.get("x-cal-signature-256", "")
    if not verify_signature(raw, signature, settings.calcom_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    body = json.loads(raw)
    trigger = body.get("triggerEvent", "")
    payload_data = body.get("payload", {})

    uid = payload_data.get("uid", "unknown")
    idem_key = f"calcom:{trigger}:{uid}"
    if db.already_processed(idem_key):
        logger.info("Duplicate Cal.com webhook %s — ignoring.", idem_key)
        return {"status": "ok", "note": "duplicate ignored"}
    db.store_event("calcom", idem_key, raw.decode("utf-8", "replace"))

    logger.info("Received Cal.com webhook: %s", trigger)
    payload = CalcomBookingPayload(**payload_data)

    if trigger == "BOOKING_CREATED":
        background.add_task(_safe, calcom_service.handle_booking_created, payload)
    elif trigger == "BOOKING_CANCELLED":
        background.add_task(_safe, calcom_service.handle_booking_cancelled, payload)
    elif trigger == "BOOKING_RESCHEDULED":
        background.add_task(_safe, calcom_service.handle_booking_rescheduled, payload)
    else:
        logger.info("Ignoring unhandled Cal.com event: %s", trigger)

    return {"status": "accepted", "event": trigger}
