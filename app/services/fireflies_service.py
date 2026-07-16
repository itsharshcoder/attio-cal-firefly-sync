"""Fireflies transcript handling (table-less; all state lives in Attio).

De-duplication and the meeting count come from the Company itself:
`dsv_fireflies_meeting_ids` holds the processed meeting ids and `dsv_meeting_count`
is their number. Resolve the account, run the analysis, write a meeting Note,
update fields, and progress the status.
"""

import asyncio
from typing import Any, Optional

from app.clients.attio import _scalar, _value, attio
from app.clients.fireflies import fetch_transcript, list_recent_transcripts
from app.config import settings
from app.logging_conf import logger
from app.models import MeetingAnalysis
from app.services import identity
from app.services.analysis import analyze_meeting

# In-process cache of meeting ids the poller has already handled this run. NOT
# persistence — the real de-dup is still the Company's dsv_fireflies_meeting_ids.
_seen_meetings: set[str] = set()

# Serialize the read-modify-write on a Company's meeting list so two webhooks for
# the SAME company can't clobber each other. Protects within one process — run a
# SINGLE uvicorn worker for full safety (Attio has no atomic increment).
_company_locks: dict[str, asyncio.Lock] = {}


def _lock_for(company_id: str) -> asyncio.Lock:
    lock = _company_locks.get(company_id)
    if lock is None:
        lock = _company_locks[company_id] = asyncio.Lock()
    return lock


def _pick_client_email(transcript: dict[str, Any], organizer_email: str) -> Optional[str]:
    """The client attendee = a participant who isn't the organizer or internal."""
    for email in transcript.get("participants") or []:
        e = (email or "").lower().strip()
        if e and e != (organizer_email or "").lower() and not identity.is_internal(e):
            return e
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
    link = transcript.get("transcript_url") or ""
    link_line = f"\n**🔗 Meeting recording / transcript:** {link}" if link else ""

    verdict = (f"**Fit:** {analysis.fit}  ·  **Type:** {analysis.category}  ·  "
               f"**Convert:** {analysis.conversion_likelihood}")

    body = f"""{verdict}
{link_line}
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

    # Correlate the meeting to an account via the client's email (People -> Company).
    company_id = await attio.find_company_for_email(client_email)
    if not company_id and settings.track_internal_meetings:
        domain = identity.domain_of(client_email)
        name = domain.split(".")[0].title() if domain else client_email
        company_id = await attio.assert_company(name=name, domain=domain)
        logger.info("Using company '%s' for internal meeting (%s)", name, client_email)
    if not company_id:
        # No matching Cal.com booking yet -> nothing to attach the meeting to.
        logger.warning("No Attio account for %s - skipping meeting %s.",
                       client_email, meeting_id)
        return {"status": "skipped", "reason": "account_not_found"}

    # De-dup + count from the Company record (no local tables). The whole
    # read-modify-write runs under a per-company lock so concurrent webhooks for
    # the same account can't lose an id or double-post a Note.
    async with _lock_for(company_id):
        values = await attio.get_record_values("companies", company_id)
        processed = [m for m in (_scalar(values, "dsv_fireflies_meeting_ids") or "").split(",") if m]
        if meeting_id in processed:
            logger.info("Meeting %s already recorded on company %s - skipping.",
                        meeting_id, company_id)
            return {"status": "ok", "action": "already_processed"}

        analysis = await analyze_meeting(transcript)

        processed.append(meeting_id)
        meeting_number = len(processed)
        title, body = _build_note(transcript, analysis, client_email, meeting_number)
        note_id = await attio.create_note(company_id, title, body)

        overview = (transcript.get("summary") or {}).get("overview", "") or ""
        summary_line = analysis.one_line_summary or (
            overview[:140] + ("…" if len(overview) > 140 else "")
        )
        new_status = _next_status(meeting_number, analysis)
        await attio.update_record("companies", company_id, {
            "dsv_fireflies_meeting_ids": _value(",".join(processed)),
            "dsv_meeting_count": _value(meeting_number),
            "dsv_fit": _value(analysis.fit),
            "dsv_client_category": _value(analysis.category),
            "dsv_conversion_likelihood": _value(analysis.conversion_likelihood),
            "dsv_next_steps": _value(analysis.next_steps),
            "dsv_last_summary": _value(summary_line),
            "dsv_last_meeting_at": _value(transcript.get("date", "")),
            "dsv_last_meeting_link": _value(transcript.get("transcript_url", "") or ""),
            "dsv_account_status": _value(new_status),
        })

    logger.info("Meeting %s (#%d) processed -> note %s on company %s, status='%s'",
                meeting_id, meeting_number, note_id, company_id, new_status)
    return {"status": "ok", "note_id": note_id, "company_id": company_id,
            "meeting_number": meeting_number, "account_status": new_status}


# ---- Auto-fetch poller (table-less) --------------------------------------

async def poll_once() -> dict:
    """Pull recent Fireflies transcripts and process any new ones.

    De-dup stays table-less (the Company's dsv_fireflies_meeting_ids). The
    in-process `_seen_meetings` set only avoids re-fetching each cycle; a meeting
    with no matching booking yet ('account_not_found') is NOT cached, so a later
    booking lets the next poll pick it up (out-of-order safe).
    """
    try:
        recent = await list_recent_transcripts(settings.fireflies_poll_limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("Fireflies poll: could not list transcripts: %s", e)
        return {"polled": 0, "new_notes": 0}

    new_notes = 0
    for t in recent:
        meeting_id = t.get("id")
        if not meeting_id or meeting_id in _seen_meetings:
            continue
        result = await handle_transcription_completed(meeting_id)
        if result.get("reason") != "account_not_found":
            _seen_meetings.add(meeting_id)
        if result.get("status") == "ok" and result.get("note_id"):
            new_notes += 1

    logger.info("Fireflies poll: scanned %d transcript(s), added %d new note(s).",
                len(recent), new_notes)
    return {"polled": len(recent), "new_notes": new_notes}


async def poll_loop() -> None:
    """Run poll_once() forever on the configured interval (started at app startup)."""
    interval = max(30, settings.fireflies_poll_seconds)
    logger.info("Fireflies auto-fetch poller running every %ds.", interval)
    while True:
        try:
            await poll_once()
        except Exception as e:  # noqa: BLE001
            logger.error("Fireflies poll loop error: %s", e)
        await asyncio.sleep(interval)
