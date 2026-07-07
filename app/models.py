"""Typed request/response models (Pydantic) used across the app."""

from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Cal.com booking payload (only the fields we use) ---

class CalcomAttendee(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    timeZone: Optional[str] = None
    phoneNumber: Optional[str] = None


class CalcomOrganizer(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None


class CalcomBookingPayload(BaseModel):
    uid: str = Field(..., description="Unique booking id (idempotency anchor)")
    title: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    organizer: Optional[CalcomOrganizer] = None
    attendees: list[CalcomAttendee] = []
    responses: dict[str, Any] = {}  # the booking-form answers

    class Config:
        extra = "allow"


# --- Normalized booking, after field extraction ---

class BookingInfo(BaseModel):
    booking_uid: str
    person_name: str
    person_email: str
    person_phone: Optional[str] = None
    company_name: str
    company_type: Optional[str] = None
    monthly_meta_spend: Optional[str] = None
    lead_source: Optional[str] = None
    prep_notes: Optional[str] = None
    start_time: Optional[str] = None
    title: Optional[str] = None
    guest_emails: list[str] = []       # extra attendees linked to the same company
    missing_required: list[str] = []   # required fields missing from the payload


# --- Structured output of the LLM meeting analysis ---

class MeetingAnalysis(BaseModel):
    fit: str = "Unknown"                     # Yes / No / Unknown
    category: str = "Unknown"               # Agency / Tool-or-Brand / Unknown
    conversion_likelihood: str = "Unknown"   # High / Medium / Low
    conversion_rationale: str = ""
    next_steps: str = ""
    one_line_summary: str = ""
