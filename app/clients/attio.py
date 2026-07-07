"""Attio API client: create/update Company & Person records, post Notes, update
fields, look up a company by attendee email, and auto-create the custom (dsv_*)
attributes on startup."""

import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.logging_conf import logger

API_BASE = "https://api.attio.com/v2"


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


# Custom attributes the app writes, created on startup if missing: (slug, title, type).
CUSTOM_ATTRIBUTES = {
    "companies": [
        ("dsv_company_type", "DSV Company Type", "text"),
        ("dsv_monthly_meta_spend", "DSV Monthly Meta Spend", "text"),
        ("dsv_lead_source", "DSV Lead Source", "text"),
        ("dsv_account_status", "DSV Account Status", "text"),
        ("dsv_calcom_booking_uids", "DSV Cal.com Booking UIDs", "text"),
        ("dsv_fit", "DSV Fit", "text"),
        ("dsv_client_category", "DSV Client Category", "text"),
        ("dsv_conversion_likelihood", "DSV Conversion Likelihood", "text"),
        ("dsv_next_steps", "DSV Next Steps", "text"),
        ("dsv_last_summary", "DSV Last Summary", "text"),
        ("dsv_last_meeting_at", "DSV Last Meeting At", "date"),
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
        """Create any missing dsv_* attributes (idempotent). Requires a token with
        object-configuration write scope; attributes that already exist are skipped."""
        if not settings.attio_api_key:
            return
        created, existing = 0, 0
        async with httpx.AsyncClient(timeout=30) as client:
            for object_slug, attrs in CUSTOM_ATTRIBUTES.items():
                for api_slug, title, attr_type in attrs:
                    body = {"data": {
                        "title": title,
                        "description": "Created by calcom-attio-sync",
                        "api_slug": api_slug,
                        "type": attr_type,
                        "is_required": False,
                        "is_unique": False,
                        "is_multiselect": False,
                        "config": {},
                        "default_value": None,
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

    async def _assert(self, object_slug: str, matching_attribute: str, values: dict) -> str:
        """Create-or-update a record on one matching attribute; return the record id."""
        url = f"{API_BASE}/objects/{object_slug}/records"
        params = {"matching_attribute": matching_attribute}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, headers=self._headers, params=params,
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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(url, headers=self._headers,
                                      json={"data": {"values": values}})
            if resp.status_code >= 400:
                logger.error("[ATTIO ERROR] %s updating %s: %s",
                             resp.status_code, object_slug, resp.text)
                resp.raise_for_status()
            logger.info("[ATTIO] updated %s/%s", object_slug, record_id)

    async def assert_company(
        self, name: str, domain: Optional[str],
        domain_unverified: bool = False, extra: Optional[dict] = None,
    ) -> str:
        """Create/update the Company, matched by domain (or by name for free emails)."""
        values: dict[str, Any] = {"name": _value(name)}
        if extra:
            values.update(extra)
        if domain:
            values["domains"] = [{"domain": domain}]
            return await self._assert("companies", "domains", values)
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

    async def find_company_for_email(self, email: str) -> Optional[str]:
        """Find a Person by email and return their linked Company id (best-effort)."""
        if not email:
            return None
        url = f"{API_BASE}/objects/people/records/query"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers=self._headers,
                                         json={"filter": {"email_addresses": email}, "limit": 1})
                if resp.status_code >= 400:
                    return None
                data = resp.json().get("data") or []
                if not data:
                    return None
                company_vals = (data[0].get("values") or {}).get("company") or []
                if company_vals:
                    return company_vals[0].get("target_record_id")
        except Exception as e:  # noqa: BLE001 - search must never break the flow
            logger.warning("[ATTIO] person search error for %s: %s", email, e)
        return None

    async def create_note(self, company_record_id: str, title: str, markdown_content: str) -> str:
        """Post a dated meeting Note onto the Company record."""
        url = f"{API_BASE}/notes"
        body = {"data": {
            "parent_object": "companies",
            "parent_record_id": company_record_id,
            "title": title,
            "format": "markdown",
            "content": markdown_content,
        }}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers, json=body)
            if resp.status_code >= 400:
                logger.error("[ATTIO ERROR] %s creating note: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            note_id = resp.json()["data"]["id"]["note_id"]
            logger.info("[ATTIO] created note %s on company %s", note_id, company_record_id)
            return note_id


attio = AttioClient()
