"""LLM analysis pass over a Fireflies transcript using Claude structured output."""

from typing import Any

from anthropic import AsyncAnthropic

from app.config import settings
from app.logging_conf import logger
from app.models import MeetingAnalysis

_SYSTEM = (
    "You are a sales analyst for DeepSolv, which sells 'Adam' — creative-decision "
    "intelligence for Meta growth teams. You read a demo/sales call summary and "
    "transcript and extract structured signals. Be concise and decisive; use "
    "'Unknown' only when there is genuinely no signal."
)


def _prompt(transcript: dict[str, Any]) -> str:
    summary = transcript.get("summary") or {}
    return (
        f"Meeting title: {transcript.get('title')}\n"
        f"Date: {transcript.get('date')}\n"
        f"Overview: {summary.get('overview')}\n"
        f"Action items: {summary.get('action_items')}\n"
        f"Keywords: {summary.get('keywords')}\n\n"
        f"Transcript excerpt:\n{transcript.get('transcript_text', '')[:6000]}\n\n"
        "Extract: fit (Yes/No/Unknown), category (Agency/Tool-or-Brand/Unknown), "
        "conversion_likelihood (High/Medium/Low), conversion_rationale (one line), "
        "next_steps (concrete follow-ups), one_line_summary (single-line highlight)."
    )


async def analyze_meeting(transcript: dict[str, Any]) -> MeetingAnalysis:
    """Return the structured analysis; falls back to 'Unknown' on any failure."""
    if not settings.anthropic_api_key:
        return MeetingAnalysis()

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.parse(
            model=settings.analysis_model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _prompt(transcript)}],
            output_format=MeetingAnalysis,
        )
        return response.parsed_output or MeetingAnalysis()
    except Exception as e:  # noqa: BLE001 - analysis must never crash the pipeline
        logger.error("Analysis failed, returning Unknown: %s", e)
        return MeetingAnalysis()
