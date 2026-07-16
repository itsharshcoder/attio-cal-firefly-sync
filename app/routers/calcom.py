"""HTTP endpoint that Cal.com POSTs to.

Flow (spec §16): verify signature -> respond 200 fast -> do the real Attio work
in the background. Attio "assert" is idempotent, so a duplicate booking webhook
simply re-upserts the same records — no local database is needed.
"""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.logging_conf import logger
from app.models import CalcomBookingPayload
from app.security import verify_signature
from app.services import calcom_service

router = APIRouter(prefix="/webhooks/cal", tags=["Cal.com"])


async def _safe(handler, payload) -> None:
    """Run a handler in the background; log a clean one-line error instead of a
    giant traceback if something fails (e.g. an Attio hiccup)."""
    try:
        await handler(payload)
    except Exception as e:  # noqa: BLE001
        logger.error("Cal.com handler %s failed for %s: %s",
                     getattr(handler, "__name__", handler), payload.uid, e)


@router.post("")
async def cal_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    # 1. Verify the request really came from Cal.com (spec §15).
    signature = request.headers.get("x-cal-signature-256", "")
    if not verify_signature(raw, signature, settings.calcom_webhook_secret):
        # 401 = Unauthorized. We refuse to process unsigned/forged requests.
        raise HTTPException(status_code=401, detail="invalid signature")

    body = json.loads(raw)
    trigger = body.get("triggerEvent", "")
    payload_data = body.get("payload", {})

    logger.info("Received Cal.com webhook: %s", trigger)

    # 2. Parse and route. Heavy work runs AFTER we return 200 (background).
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
