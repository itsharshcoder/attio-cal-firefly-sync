"""Attio API client (table-less: all state lives in Attio).

Provides: assert Company/Person, update fields, create Notes, read a record's
values, look up a Person by email, find a same-named person at a company, append
an email to a Person, and auto-create the custom (dsv_*) attributes on startup.
"""

import asyncio
import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.logging_conf import logger

API_BASE = "https://api.attio.com/v2"

# Transient statuses worth retrying (rate limit + gateway/backend blips).
_RETRYABLE = {429, 502, 503, 504}


def _retry_after(resp: Any) -> Optional[float]:
    """Seconds to wait from a Retry-After header, if present and numeric."""
    headers = getattr(resp, "headers", {}) or {}
    try:
        return float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None


def _value(v: Any) -> list[dict]:
    """Wrap a scalar in Attio's value format."""
    return [{"value": v}]


def _person_name(full_name: str) -> list[dict]:
    """Format a name for Attio's People `name` (personal-name) attribute."""
    parts = (full_name or "").strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return [{"first_name": first, "last_name": last, "full_name": full_name}]


def _valid_phone(phone: str) -> bool:
    """True for a plausible international number (leading '+' and >= 8 digits)."""
    if not phone:
        return False
    digits = re.sub(r"\D", "", phone)
    return phone.strip().startswith("+") and len(digits) >= 8


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _scalar(values: dict, slug: str) -> Optional[str]:
    """Read a single scalar attribute value from a record's `values` dict."""
    arr = values.get(slug) or []
    if arr and isinstance(arr[0], dict):
        return arr[0].get("value")
    return None


# Custom attributes created on startup if missing: (slug, title, Attio type).
CUSTOM_ATTRIBUTES = {
    "companies": [
        ("dsv_company_type", "DSV Company Type", "text"),
        ("dsv_monthly_meta_spend", "DSV Monthly Meta Spend", "text"),
        ("dsv_lead_source", "DSV Lead Source", "text"),
        ("dsv_account_status", "DSV Account Status", "text"),
        ("dsv_calcom_booking_uids", "DSV Cal.com Booking UIDs", "text"),
        ("dsv_fireflies_meeting_ids", "DSV Fireflies Meeting IDs", "text"),
        ("dsv_fit", "DSV Fit", "text"),
        ("dsv_client_category", "DSV Client Category", "text"),
        ("dsv_conversion_likelihood", "DSV Conversion Likelihood", "text"),
        ("dsv_next_steps", "DSV Next Steps", "text"),
        ("dsv_last_summary", "DSV Last Summary", "text"),
        ("dsv_last_meeting_at", "DSV Last Meeting At", "date"),
        ("dsv_last_meeting_link", "DSV Last Meeting Link", "text"),
        ("dsv_meeting_count", "DSV Meeting Count", "number"),
        ("dsv_domain_unverified", "DSV Domain Unverified", "checkbox"),
        ("dsv_needs_review", "DSV Needs Review", "checkbox"),
    ],
    "people": [
        ("dsv_source", "DSV Source", "text"),
        ("dsv_lead_source", "DSV Lead Source", "text"),
        ("dsv_prep_notes", "DSV Prep Notes", "text"),
        ("dsv_needs_review", "DSV Needs Review", "checkbox"),
    ],
}


class AttioClient:
    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.attio_api_key}",
            "Content-Type": "application/json",
        }

    async def ensure_custom_attributes(self) -> None:
        """Create any missing dsv_* attributes (idempotent). Needs a token with
        object-configuration write scope; existing attributes are skipped."""
        if not settings.attio_api_key:
            return
        created, existing = 0, 0
        async with httpx.AsyncClient(timeout=30) as client:
            for object_slug, attrs in CUSTOM_ATTRIBUTES.items():
                for api_slug, title, attr_type in attrs:
                    body = {"data": {
                        "title": title, "description": "Created by calcom-attio-sync",
                        "api_slug": api_slug, "type": attr_type,
                        "is_required": False, "is_unique": False,
                        "is_multiselect": False, "config": {}, "default_value": None,
                    }}
                    resp = await client.post(
                        f"{API_BASE}/objects/{object_slug}/attributes",
                        headers=self._headers, json=body,
                    )
                    if resp.status_code < 300:
                        created += 1
                    elif resp.status_code in (400, 409) and (
                        "already" in resp.text.lower() or "conflict" in resp.text.lower()
                        or "slug" in resp.text.lower()
                    ):
                        existing += 1
                    else:
                        logger.warning("[ATTIO] attribute %s.%s not ensured (%s): %s",
                                       object_slug, api_slug, resp.status_code, resp.text[:200])
        logger.info("[ATTIO] custom attributes ready (created=%d, existing=%d).",
                    created, existing)

    # ---- HTTP with retry --------------------------------------------------

    async def _http(self, method: str, url: str, *, params: Optional[dict] = None,
                    json: Optional[dict] = None, retries: int = 3) -> httpx.Response:
        """Send one request, retrying transient Attio errors (429 / 5xx) with
        backoff that honors Retry-After. Returns the final response."""
        attempt = 0
        while True:
            async with httpx.AsyncClient(timeout=30) as client:
                kwargs: dict[str, Any] = {"headers": self._headers}
                if params is not None:
                    kwargs["params"] = params
                if json is not None:
                    kwargs["json"] = json
                resp = await getattr(client, method)(url, **kwargs)
            if resp.status_code in _RETRYABLE and attempt < retries:
                attempt += 1
                delay = _retry_after(resp) or min(2 ** attempt, 8)
                logger.warning("[ATTIO] %s %s -> %s; retry %d/%d in %.1fs",
                               method.upper(), url, resp.status_code, attempt, retries, delay)
                await asyncio.sleep(delay)
                continue
            return resp

    # ---- Core record operations -------------------------------------------

    async def _assert(self, object_slug: str, matching_attribute: str, values: dict) -> str:
        """Create-or-update a record on one matching attribute; return the record id."""
        url = f"{API_BASE}/objects/{object_slug}/records"
        resp = await self._http("put", url,
                                params={"matching_attribute": matching_attribute},
                                json={"data": {"values": values}})
        if resp.status_code >= 400:
            logger.error("[ATTIO ERROR] %s asserting %s: %s",
                         resp.status_code, object_slug, resp.text)
            resp.raise_for_status()
        record_id = resp.json()["data"]["id"]["record_id"]
        logger.info("[ATTIO] asserted %s -> %s", object_slug, record_id)
        return record_id

    async def update_record(self, object_slug: str, record_id: str, values: dict) -> None:
        """Update fields on an existing record."""
        url = f"{API_BASE}/objects/{object_slug}/records/{record_id}"
        resp = await self._http("patch", url, json={"data": {"values": values}})
        if resp.status_code >= 400:
            logger.error("[ATTIO ERROR] %s updating %s: %s",
                         resp.status_code, object_slug, resp.text)
            resp.raise_for_status()
        logger.info("[ATTIO] updated %s/%s", object_slug, record_id)

    async def get_record_values(self, object_slug: str, record_id: str) -> dict:
        """Return a record's `values` dict (used to read current dsv_* fields)."""
        url = f"{API_BASE}/objects/{object_slug}/records/{record_id}"
        resp = await self._http("get", url)
        if resp.status_code >= 400:
            return {}
        return resp.json().get("data", {}).get("values", {})

    # ---- Company / Person -------------------------------------------------

    async def assert_company(
        self, name: str, domain: Optional[str],
        domain_unverified: bool = False, extra: Optional[dict] = None,
    ) -> str:
        """Create/update the Company, matched by domain (or by name for free emails)."""
        values: dict[str, Any] = {}
        # Only write the name when it's meaningful, so a later booking that left the
        # company field blank ("Unknown") can't overwrite a good existing name.
        has_name = bool(name) and name.strip().lower() != "unknown"
        if has_name:
            values["name"] = _value(name)
        if extra:
            values.update(extra)
        if domain:
            values["domains"] = [{"domain": domain}]
            return await self._assert("companies", "domains", values)
        # Free-email path matches on the name, so a name must be present here.
        values.setdefault("name", _value(name))
        if domain_unverified:
            values["dsv_domain_unverified"] = _value(True)
        return await self._assert("companies", "name", values)

    async def assert_person(
        self, name: str, email: str, phone: Optional[str],
        company_record_id: str, extra: Optional[dict] = None,
    ) -> str:
        """Create/update the Person (matched by email) and link it to the Company."""
        values: dict[str, Any] = {
            "name": _person_name(name),
            "email_addresses": [{"email_address": email}],
            "company": [
                {"target_object": "companies", "target_record_id": company_record_id}
            ],
        }
        if phone and _valid_phone(phone):
            values["phone_numbers"] = [{"original_phone_number": phone.strip()}]
        elif phone:
            logger.info("[ATTIO] Skipping invalid phone '%s'.", phone)
        if extra:
            values.update(extra)
        return await self._assert("people", "email_addresses", values)

    async def upsert_person(
        self, name: str, email: str, phone: Optional[str],
        company_id: str, extra: Optional[dict] = None,
    ) -> tuple[str, bool]:
        """Create/update a Person, handling the same-person-different-email case.

        1. Email matches an existing Person -> update it (email is the reliable key).
        2. No email match, but the same name already exists at this Company ->
           the same human with a new email; append the email and flag for review.
        3. Otherwise -> create a new Person.
        Returns (person_id, merged_by_name).
        """
        existing = await self.find_person_by_email(email)
        if existing:
            vals: dict[str, Any] = {
                "name": _person_name(name),
                "company": [{"target_object": "companies", "target_record_id": company_id}],
            }
            if phone and _valid_phone(phone):
                vals["phone_numbers"] = [{"original_phone_number": phone.strip()}]
            if extra:
                vals.update(extra)
            await self.update_record("people", existing["id"], vals)  # keeps email list intact
            return existing["id"], False

        same = await self.find_person_by_name_at_company(name, company_id)
        if same:
            await self.append_person_email(same["id"], same["emails"], email)
            vals = {"company": [{"target_object": "companies", "target_record_id": company_id}],
                    "dsv_needs_review": _value(True)}
            if extra:
                vals.update(extra)
            await self.update_record("people", same["id"], vals)
            logger.info("[ATTIO] same-name person at company -> appended email %s (review)", email)
            return same["id"], True

        pid = await self.assert_person(name, email, phone, company_id, extra)
        return pid, False

    async def find_person_by_email(self, email: str) -> Optional[dict]:
        """Find a Person by email; return {id, emails, company_id} or None."""
        if not email:
            return None
        url = f"{API_BASE}/objects/people/records/query"
        try:
            resp = await self._http("post", url,
                                    json={"filter": {"email_addresses": email}, "limit": 1})
            if resp.status_code >= 400:
                return None
            data = resp.json().get("data") or []
            if not data:
                return None
            return _person_summary(data[0])
        except Exception as e:  # noqa: BLE001
            logger.warning("[ATTIO] person-by-email search error: %s", e)
            return None

    async def find_company_for_email(self, email: str) -> Optional[str]:
        """Return the Company id linked to the Person with this email (or None)."""
        person = await self.find_person_by_email(email)
        return person["company_id"] if person else None

    async def find_person_by_name_at_company(self, name: str, company_id: str) -> Optional[dict]:
        """Find a Person with the same (normalized) name already at this Company.
        Used to catch the same human booking again with a different email."""
        if not name or not company_id:
            return None
        url = f"{API_BASE}/objects/people/records/query"
        target = _norm_name(name)
        try:
            resp = await self._http("post", url, json={
                "filter": {"company": {"target_record_id": company_id}}, "limit": 50})
            if resp.status_code >= 400:
                return None
            for p in (resp.json().get("data") or []):
                summ = _person_summary(p)
                if _norm_name(summ["name"]) == target:
                    return summ
        except Exception as e:  # noqa: BLE001
            logger.warning("[ATTIO] person-by-name search error: %s", e)
        return None

    async def append_person_email(self, person_id: str, emails: list[str], new_email: str) -> None:
        """Add a new email to an existing Person (email_addresses is multi-value)."""
        all_emails = list(dict.fromkeys([*emails, new_email]))  # de-dupe, keep order
        await self.update_record("people", person_id, {
            "email_addresses": [{"email_address": e} for e in all_emails],
        })

    # ---- Notes ------------------------------------------------------------

    async def create_note(self, company_record_id: str, title: str, markdown_content: str) -> str:
        """Post a dated meeting Note onto the Company record."""
        url = f"{API_BASE}/notes"
        body = {"data": {
            "parent_object": "companies", "parent_record_id": company_record_id,
            "title": title, "format": "markdown", "content": markdown_content,
        }}
        resp = await self._http("post", url, json=body)
        if resp.status_code >= 400:
            logger.error("[ATTIO ERROR] %s creating note: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        note_id = resp.json()["data"]["id"]["note_id"]
        logger.info("[ATTIO] created note %s on company %s", note_id, company_record_id)
        return note_id


def _person_summary(record: dict) -> dict:
    """Pull {id, name, emails, company_id} out of an Attio person record."""
    values = record.get("values") or {}
    name_arr = values.get("name") or []
    name = name_arr[0].get("full_name") if name_arr else ""
    emails = [e.get("email_address") for e in (values.get("email_addresses") or []) if e.get("email_address")]
    comp = values.get("company") or []
    company_id = comp[0].get("target_record_id") if comp else None
    return {"id": record["id"]["record_id"], "name": name, "emails": emails, "company_id": company_id}


attio = AttioClient()
