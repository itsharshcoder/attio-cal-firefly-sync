"""Fireflies transcript handling: resolve the account, run the analysis, write a
meeting Note, update fields, bump the meeting count, and progress the status."""

from typing import Any, Optional

from app import db
from app.clients.attio import _value, attio
from app.clients.fireflies import fetch_transcript
from app.config import settings
from app.logging_conf import logger
from app.models import MeetingAnalysis
from app.services import identity
from app.services.analysis import analyze_meeting


def _pick_client_email(transcript: dict[str, Any], organizer_email: str) -> Optional[str]:
    """The client attendee = a participant who isn't the organizer or internal."""
    for email in transcript.get("participants") or []:
        e = (email or "").lower().strip()
        if e and e != (organizer_email or "").lower() and not identity.is_internal(e):
            return e
    return None


async def _resolve_company(client_email: str) -> Optional[str]:
    """Find the Company for an attendee: local cache, then Attio search, then domain."""
    company_id = db.cache_get(f"company:for-email:{client_email}")
    if company_id:
        return company_id
    company_id = await attio.find_company_for_email(client_email)
    if company_id:
        db.cache_put(f"company:for-email:{client_email}", company_id)
        return company_id
    domain = identity.domain_of(client_email)
    if domain and not identity.is_free_email(client_email):
        return db.cache_get(f"company:domain:{domain}")
    return None


def _next_status(meeting_number: int, analysis: MeetingAnalysis) -> str:
    """Progress the account status as meetings accumulate."""
    if analysis.fit.strip().lower() == "no":
        return "Disqualified"
    if meeting_number <= 1:
        return "Demo completed"
    return "Evaluating"


def _build_note(transcript: dict, analysis: MeetingAnalysis,
                client_email: str, meeting_number: int) -> tuple[str, str]:
    """Build a scannable meeting Note (title, markdown body)."""
    date = transcript.get("date", "")
    summary = transcript.get("summary") or {}
    title = f"Meeting #{meeting_number} with {client_email} — {date}"

    action_items = summary.get("action_items") or []
    action_lines = "\n".join(f"{i}. {a}" for i, a in enumerate(action_items, 1)) or "_none_"
    point_lines = "\n".join(f"- {p}" for p in (summary.get("bullet_gist") or [])) or "_none_"
    keywords = ", ".join(summary.get("keywords") or []) or "—"

    verdict = (f"**Fit:** {analysis.fit}  ·  **Type:** {analysis.category}  ·  "
               f"**Convert:** {analysis.conversion_likelihood}")

    body = f"""{verdict}

**Next steps:** {analysis.next_steps or '_TBD_'}

---

### 📝 Summary
{summary.get('overview', '_(no overview)_')}

### 🔑 Key points
{point_lines}

### ✅ Action items
{action_lines}

### 👥 Attendees
- **{client_email}** — client (this meeting is tracked by this email)
- {transcript.get('organizer_email', '')} — DeepSolv

---
_Meeting #{meeting_number} · {date} · Keywords: {keywords}_
"""
    return title, body


async def handle_transcription_completed(meeting_id: str) -> dict:
    """Process one 'Transcription completed' event end to end."""
    if db.get_note_id(meeting_id):
        logger.info("Meeting %s already has a Note - skipping.", meeting_id)
        return {"status": "ok", "action": "already_processed"}

    transcript = await fetch_transcript(meeting_id)
    organizer_email = transcript.get("organizer_email", "")

    client_email = _pick_client_email(transcript, organizer_email)
    if not client_email:
        # No external client. Skip for real behavior; for testing/demo
        # (TRACK_INTERNAL_MEETINGS=true) track it under the organizer's company.
        if settings.track_internal_meetings:
            client_email = organizer_email or (transcript.get("participants") or [None])[0]
            logger.info("No external client; TRACK_INTERNAL_MEETINGS on -> tracking as %s",
                        client_email)
        if not client_email:
            logger.info("Meeting %s is internal-only - skipping.", meeting_id)
            return {"status": "ok", "action": "internal_only_skipped"}

    company_id = await _resolve_company(client_email)
    if not company_id and settings.track_internal_meetings:
        domain = identity.domain_of(client_email)
        name = domain.split(".")[0].title() if domain else client_email
        company_id = await attio.assert_company(name=name, domain=domain)
        db.cache_put(f"company:for-email:{client_email}", company_id)
        logger.info("Using company '%s' for internal meeting (%s)", name, client_email)
    if not company_id:
        # Booking not processed yet: park and retry when it arrives.
        db.add_pending_fireflies(meeting_id, client_email)
        logger.warning("No Company yet for %s - parked meeting %s for retry.",
                       client_email, meeting_id)
        return {"status": "parked_for_retry", "reason": "company_not_found"}

    analysis = await analyze_meeting(transcript)

    meeting_number = db.bump_meeting_count(company_id)
    title, body = _build_note(transcript, analysis, client_email, meeting_number)
    note_id = await attio.create_note(company_id, title, body)
    db.save_note_id(meeting_id, note_id)

    overview = (transcript.get("summary") or {}).get("overview", "") or ""
    summary_line = analysis.one_line_summary or (
        overview[:140] + ("…" if len(overview) > 140 else "")
    )
    new_status = _next_status(meeting_number, analysis)
    await attio.update_record("companies", company_id, {
        "dsv_fit": _value(analysis.fit),
        "dsv_client_category": _value(analysis.category),
        "dsv_conversion_likelihood": _value(analysis.conversion_likelihood),
        "dsv_next_steps": _value(analysis.next_steps),
        "dsv_last_summary": _value(summary_line),
        "dsv_last_meeting_at": _value(transcript.get("date", "")),
        "dsv_meeting_count": _value(meeting_number),
        "dsv_account_status": _value(new_status),
    })

    logger.info("Meeting %s (#%d) processed -> note %s on company %s, status='%s'",
                meeting_id, meeting_number, note_id, company_id, new_status)
    return {"status": "ok", "note_id": note_id, "company_id": company_id,
            "meeting_number": meeting_number, "account_status": new_status}
