"""Webhook signature verification (spec §15).

A webhook is just an HTTP POST to a public URL. Anyone who finds your URL could
POST fake data. To prove a request really came from Cal.com / Fireflies, each
provider signs the request body with a shared secret using HMAC-SHA256, and puts
the result in a header. We recompute the same signature and compare.

If they match -> genuine. If not -> reject with 401.
"""

import hashlib
import hmac

from app.config import settings
from app.logging_conf import logger


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return True if `signature` is a valid HMAC-SHA256 of `raw_body` using `secret`.

    Fail closed: if no secret is configured (blank in .env) the request is REJECTED,
    unless ALLOW_UNSIGNED_WEBHOOKS=true is set for local testing.
    """
    if not secret:
        if settings.allow_unsigned_webhooks:
            logger.warning("No webhook secret set; ALLOW_UNSIGNED_WEBHOOKS=true — "
                           "accepting request (DEV ONLY, do not use in production).")
            return True
        logger.error("Rejecting webhook: no secret configured and "
                     "ALLOW_UNSIGNED_WEBHOOKS=false. Set the secret (or the flag for local tests).")
        return False

    if not signature:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # compare_digest avoids subtle timing attacks vs a plain `==`.
    return hmac.compare_digest(expected, signature)
