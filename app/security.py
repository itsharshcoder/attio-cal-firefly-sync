"""Webhook signature verification (HMAC-SHA256)."""

import hashlib
import hmac

from app.logging_conf import logger


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return True if `signature` is a valid HMAC-SHA256 of `raw_body` with `secret`.

    If no secret is configured the check is skipped (returns True).
    """
    if not secret:
        logger.warning("No webhook secret configured — skipping signature check.")
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
