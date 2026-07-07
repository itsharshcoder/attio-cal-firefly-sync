"""Application configuration, loaded from the .env file."""

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Cal.com
    calcom_webhook_secret: str = ""
    calcom_api_key: str = ""

    # Fireflies
    fireflies_webhook_secret: str = ""
    fireflies_api_key: str = ""

    # Attio
    attio_api_key: str = ""

    # Claude (meeting analysis)
    anthropic_api_key: str = ""
    analysis_model: str = "claude-opus-4-8"

    # Comma-separated internal team domains (never treated as the client).
    internal_email_domains: str = "deepsolv.com,deepsolv.ai"

    # When True, internal-only meetings also get a note (on the organizer's
    # company). Useful for testing/demos; keep False for real behavior.
    track_internal_meetings: bool = False

    port: int = 8000

    @cached_property
    def internal_domains_set(self) -> set[str]:
        """Internal domains as a lowercased set for fast lookups."""
        return {
            d.strip().lower()
            for d in self.internal_email_domains.split(",")
            if d.strip()
        }


settings = Settings()
