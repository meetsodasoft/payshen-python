"""Webhook deliveries: verify, parse, and take each event once.

    raw = request.get_data()   # the RAW body (Flask); Django: request.body
    try:
        event = payshen.parse_webhook_event(raw, request.headers.get("X-Payshen-Signature"), secret)
    except payshen.SignatureVerificationError:
        return "", 400

The signature proves a delivery came from Payshen and is recent. It does not
make it new: deliveries are retried, so deduplicate on ``event["id"]`` (or use
``claim_webhook_event``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Sequence, Union

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment,misc]

from .errors import SignatureVerificationError

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "verify_webhook_signature",
    "parse_webhook_event",
    "claim_webhook_event",
    "WebhookEventStore",
    "MemoryWebhookEventStore",
]

DEFAULT_TOLERANCE_SECONDS = 300

Body = Union[bytes, str]
Secret = Union[str, Sequence[str]]


def _bytes(body: Body) -> bytes:
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def verify_webhook_signature(
    body: Body,
    header: Optional[str],
    secret: Secret,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> None:
    """Check ``X-Payshen-Signature`` (``t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">``).

    ``secret`` may be a list during a rotation. Raises ``SignatureVerificationError``.
    """
    if not header:
        raise SignatureVerificationError("missing_signature", "Missing X-Payshen-Signature header.")
    parts: Dict[str, str] = {}
    for part in header.split(","):
        key, _, value = part.partition("=")
        parts[key.strip()] = value.strip()
    t = parts.get("t", "")
    v1 = parts.get("v1", "")
    if not (t.isascii() and t.isdigit()) or not v1:
        raise SignatureVerificationError("malformed_signature", "Signature is malformed.")

    current = int(time.time()) if now is None else now
    if abs(current - int(t)) > tolerance_seconds:
        raise SignatureVerificationError("expired_signature", "Signature timestamp outside tolerance window.")

    secrets = [secret] if isinstance(secret, str) else list(secret)
    if not secrets:
        raise SignatureVerificationError("missing_secret", "No signing secret was provided.")

    signed = t.encode("ascii") + b"." + _bytes(body)
    # Every candidate is checked, timing-safe each time, so a rotation does not
    # turn into an oracle for which secret is live.
    matched = False
    for candidate in secrets:
        expected = hmac.new(candidate.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, v1):
            matched = True
    if not matched:
        raise SignatureVerificationError("invalid_signature", "Signature does not match payload.")


def parse_webhook_event(
    body: Body,
    header: Optional[str],
    secret: Secret,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Verify, then return the event (``id``, ``type``, ``created``, ``data``)."""
    verify_webhook_signature(body, header, secret, tolerance_seconds=tolerance_seconds, now=now)
    try:
        event = json.loads(_bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SignatureVerificationError("malformed_event", "Verified body is not valid JSON.") from None
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
        raise SignatureVerificationError("malformed_event", "Verified body is not a Payshen event envelope.")
    return event


class WebhookEventStore(Protocol):
    """Where you record which events you have already handled.

    One atomic ``claim`` rather than a "seen" and "remember" pair: between a
    read and a write a second delivery reads "not seen" too, and both handlers
    proceed. Your database does it in one statement:
    ``INSERT ... ON CONFLICT (id) DO NOTHING RETURNING id`` (PostgreSQL),
    ``INSERT IGNORE`` and the affected row count (MySQL), ``SET id 1 NX`` (Redis).
    """

    def claim(self, event_id: str) -> bool:  # pragma: no cover - interface
        """True when THIS call recorded the id (do the work), False when it was already there."""
        ...


class MemoryWebhookEventStore:
    """In-process, for tests and local development. Wrong in production: it is
    not shared between workers and forgets on restart."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max = max_entries
        self._seen: Dict[str, None] = {}

    def claim(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        if len(self._seen) >= self._max:
            del self._seen[next(iter(self._seen))]
        self._seen[event_id] = None
        return True


def claim_webhook_event(
    body: Body,
    header: Optional[str],
    secret: Secret,
    store: WebhookEventStore,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Verify, parse and claim: the event the first time, ``None`` for a duplicate (answer it 2xx).

    The claim happens BEFORE your work: if the process dies in between, the
    event is marked handled and never was. Where that matters, claim and work
    in one transaction with your own store instead.
    """
    event = parse_webhook_event(body, header, secret, tolerance_seconds=tolerance_seconds, now=now)
    return event if store.claim(event["id"]) else None
