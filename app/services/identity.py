"""Field extraction from the Cal.com payload and identity/de-duplication rules.

Rules: Person is keyed by email; Company is keyed by work-email domain, or by
name for free/personal emails. Free-provider domains are never company domains.
"""

from typing import Any, Optional

from app.config import settings
from app.models import BookingInfo, CalcomBookingPayload

FREE_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "protonmail.com", "aol.com", "live.com", "msn.com", "gmx.com",
    "yahoo.co.in", "rediffmail.com",
}

# Role/shared mailboxes — one inbox is not one person.
ROLE_MAILBOX_PREFIXES = {"info", "sales", "hello", "team", "contact", "support", "admin"}

# Obviously-invalid company names to flag for review.
_JUNK_NAMES = {"test", "testing", "asdf", "na", "n/a", "none", "-", "."}


def domain_of(email: str) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


def local_part(email: str) -> str:
    return email.split("@", 1)[0].strip().lower() if email and "@" in email else ""


def is_free_email(email: str) -> bool:
    d = domain_of(email)
    return d in FREE_EMAIL_DOMAINS if d else True


def is_internal(email: str) -> bool:
    """True if the email belongs to our own team (an internal domain)."""
    d = domain_of(email)
    return d in settings.internal_domains_set if d else False


def is_role_mailbox(email: str) -> bool:
    return local_part(email) in ROLE_MAILBOX_PREFIXES


def is_junk_company(name: str) -> bool:
    n = (name or "").strip().lower()
    return (not n) or n in _JUNK_NAMES or len(n) <= 1


def _clean(value: Any) -> Optional[str]:
    """Normalize a Cal.com answer that may be a str, {'value': ...}, or a list."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value", value.get("label"))
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    text = str(value).strip()
    return text or None


def _norm_key(key: str) -> str:
    return key.lower().replace("_", "-").replace(" ", "-")


def _find(responses: dict, include_all: list[str], exclude_any: list[str] = ()) -> Optional[str]:
    """Return the first form field whose key contains all `include_all` words and
    none of `exclude_any` (disambiguates similar keys)."""
    for key, raw in responses.items():
        k = _norm_key(key)
        if all(w in k for w in include_all) and not any(w in k for w in exclude_any):
            val = _clean(raw)
            if val is not None:
                return val
    return None


def _extract_guests(payload: CalcomBookingPayload, booker_email: str) -> list[str]:
    """Guest emails from responses.guests and any extra (external) attendees."""
    guests: list[str] = []
    raw_guests = (payload.responses or {}).get("guests")
    if isinstance(raw_guests, list):
        guests.extend(str(g).strip().lower() for g in raw_guests if g and "@" in str(g))
    for att in payload.attendees[1:]:
        e = (att.email or "").strip().lower()
        if e and e != booker_email and not is_internal(e):
            guests.append(e)
    seen, out = set(), []
    for g in guests:
        if g and g != booker_email and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def extract_booking_info(payload: CalcomBookingPayload) -> BookingInfo:
    """Map the raw Cal.com payload to a clean BookingInfo, flagging missing fields."""
    responses = payload.responses or {}
    attendee = payload.attendees[0] if payload.attendees else None

    name = (
        _find(responses, ["name"], exclude_any=["company", "first", "last", "user"])
        or (attendee.name if attendee else None) or "Unknown"
    )
    email = (
        _find(responses, ["email"]) or (attendee.email if attendee else None) or ""
    ).lower().strip()
    phone = _find(responses, ["phone"]) or (attendee.phoneNumber if attendee else None)

    company_name = (
        _find(responses, ["company", "name"])
        or _find(responses, ["organisation"])
        or _find(responses, ["organization"])
        or _find(responses, ["company"], exclude_any=["describe", "type", "best", "spend"])
        or "Unknown"
    )
    company_type = (
        _find(responses, ["describe", "company"])
        or _find(responses, ["company", "type"])
        or _find(responses, ["company", "best"])
    )
    monthly_spend = _find(responses, ["spend"])
    lead_source = _find(responses, ["hear"]) or _find(responses, ["source"])
    prep_notes = (
        _find(responses, ["help", "prepare"])
        or _find(responses, ["anything", "else"])
        or _find(responses, ["prepare"])
        or _find(responses, ["notes"])
    )

    missing = []
    if name == "Unknown":
        missing.append("name")
    if not email:
        missing.append("email")
    if company_name == "Unknown":
        missing.append("company_name")
    if not company_type:
        missing.append("company_type")
    if not monthly_spend:
        missing.append("monthly_meta_spend")
    if not lead_source:
        missing.append("lead_source")

    return BookingInfo(
        booking_uid=payload.uid,
        person_name=name,
        person_email=email,
        person_phone=phone,
        company_name=company_name,
        company_type=company_type,
        monthly_meta_spend=monthly_spend,
        lead_source=lead_source,
        prep_notes=prep_notes,
        start_time=payload.startTime,
        title=payload.title,
        guest_emails=_extract_guests(payload, email),
        missing_required=missing,
    )


def resolve_company_key(email: str, company_name: str) -> tuple[Optional[str], bool]:
    """Return (domain_or_None, domain_unverified): use the work-email domain, or
    fall back to name matching (unverified) for free/personal emails."""
    if email and not is_free_email(email):
        return domain_of(email), False
    return None, True
