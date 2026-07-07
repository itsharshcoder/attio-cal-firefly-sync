"""Cal.com booking handling: resolve Company, then Person (+ guests), set the
initial status, and auto-retry any Fireflies summary that arrived early."""

from app import db
from app.clients.attio import _value, attio
from app.logging_conf import logger
from app.models import BookingInfo, CalcomBookingPayload
from app.services import identity


async def handle_booking_created(payload: CalcomBookingPayload) -> dict:
    """Create/update Company + Person (+ guests) from a new booking."""
    info: BookingInfo = identity.extract_booking_info(payload)
    logger.info("Processing booking uid=%s person=%s company=%s guests=%d",
                info.booking_uid, info.person_email or "(no email)",
                info.company_name, len(info.guest_emails))

    # Decide whether a human should review this booking.
    reasons = []
    if identity.is_internal(info.person_email):
        reasons.append("internal teammate booker")
    if info.missing_required:
        reasons.append("missing: " + ", ".join(info.missing_required))
    if identity.is_junk_company(info.company_name):
        reasons.append(f"junk company name '{info.company_name}'")
    if identity.is_role_mailbox(info.person_email):
        reasons.append(f"role mailbox {info.person_email}")
    needs_review = bool(reasons)
    if needs_review:
        logger.warning("Booking %s flagged for review: %s",
                       info.booking_uid, "; ".join(reasons))

    # 1) Company (matched by work-email domain, or by name for free emails).
    domain, domain_unverified = identity.resolve_company_key(
        info.person_email, info.company_name
    )
    company_extra = {"dsv_account_status": _value("New - Demo booked"),
                     "dsv_calcom_booking_uids": _value(info.booking_uid)}
    if info.company_type:
        company_extra["dsv_company_type"] = _value(info.company_type)
    if info.monthly_meta_spend:
        company_extra["dsv_monthly_meta_spend"] = _value(info.monthly_meta_spend)
    if info.lead_source:
        company_extra["dsv_lead_source"] = _value(info.lead_source)
    if needs_review:
        company_extra["dsv_needs_review"] = _value(True)

    company_id = await attio.assert_company(
        name=info.company_name, domain=domain,
        domain_unverified=domain_unverified, extra=company_extra,
    )

    # 2) Person (matched by email), linked to the Company.
    person_extra = {"dsv_source": _value("Cal.com booking")}
    if info.lead_source:
        person_extra["dsv_lead_source"] = _value(info.lead_source)
    if info.prep_notes:
        person_extra["dsv_prep_notes"] = _value(info.prep_notes)
    if needs_review:
        person_extra["dsv_needs_review"] = _value(True)

    person_id = None
    if info.person_email:
        person_id = await attio.assert_person(
            name=info.person_name, email=info.person_email, phone=info.person_phone,
            company_record_id=company_id, extra=person_extra,
        )

    # 2b) Guests -> extra People on the same Company.
    for guest_email in info.guest_emails:
        await attio.assert_person(
            name=guest_email.split("@")[0].title(), email=guest_email, phone=None,
            company_record_id=company_id,
            extra={"dsv_source": _value("Cal.com booking (guest)")},
        )
        db.cache_put(f"company:for-email:{guest_email}", company_id)

    # 3) Cache identity -> company so the Fireflies step can correlate later.
    if info.person_email:
        db.cache_put(f"company:for-email:{info.person_email}", company_id)
        db.cache_put(f"person:email:{info.person_email}", person_id or "")
    if domain:
        db.cache_put(f"company:domain:{domain}", company_id)

    # 4) Auto-retry any Fireflies summary that arrived before this booking.
    from app.services import fireflies_service  # local import avoids a cycle
    for e in [info.person_email, *info.guest_emails]:
        if not e:
            continue
        for meeting_id in db.take_pending_fireflies_for_email(e):
            logger.info("Retrying parked Fireflies meeting %s for %s", meeting_id, e)
            await fireflies_service.handle_transcription_completed(meeting_id)

    logger.info("Booking done: company=%s person=%s guests=%d review=%s",
                company_id, person_id, len(info.guest_emails), needs_review)
    return {"status": "ok", "company_id": company_id, "person_id": person_id,
            "guests": info.guest_emails, "needs_review": needs_review,
            "booking_uid": info.booking_uid}


async def handle_booking_cancelled(payload: CalcomBookingPayload) -> dict:
    logger.info("Booking cancelled: uid=%s", payload.uid)
    return {"status": "ok", "action": "cancellation_logged", "booking_uid": payload.uid}


async def handle_booking_rescheduled(payload: CalcomBookingPayload) -> dict:
    logger.info("Booking rescheduled: uid=%s", payload.uid)
    return {"status": "ok", "action": "reschedule_logged", "booking_uid": payload.uid}
