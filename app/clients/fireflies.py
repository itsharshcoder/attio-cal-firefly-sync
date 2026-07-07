"""Fireflies GraphQL client: fetch a meeting transcript + summary by id."""

from typing import Any

import httpx

from app.config import settings
from app.logging_conf import logger

API_URL = "https://api.fireflies.ai/graphql"

_TRANSCRIPT_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    organizer_email
    participants
    meeting_attendees {
      email
      displayName
    }
    summary {
      overview
      action_items
      keywords
      bullet_gist
    }
    sentences {
      speaker_name
      text
    }
  }
}
"""


async def fetch_transcript(meeting_id: str) -> dict[str, Any]:
    """Return the transcript (title, date, organizer, participants, summary) plus a
    flattened `transcript_text` for the analysis step."""
    headers = {
        "Authorization": f"Bearer {settings.fireflies_api_key}",
        "Content-Type": "application/json",
    }
    body = {"query": _TRANSCRIPT_QUERY, "variables": {"id": meeting_id}}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(API_URL, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()

    if "errors" in payload:
        raise RuntimeError(f"Fireflies GraphQL error: {payload['errors']}")

    transcript = payload["data"]["transcript"]

    # Merge meeting_attendees emails into participants (more complete list).
    emails = [e for e in (transcript.get("participants") or []) if e]
    for a in (transcript.get("meeting_attendees") or []):
        e = (a or {}).get("email")
        if e and e not in emails:
            emails.append(e)
    transcript["participants"] = emails

    sentences = transcript.get("sentences") or []
    transcript["transcript_text"] = "\n".join(
        f"{s.get('speaker_name', '?')}: {s.get('text', '')}" for s in sentences
    )
    return transcript
