"""Signature checks against headers made the way Payshen makes them."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable

import pytest

from payshen import (
    MemoryWebhookEventStore,
    SignatureVerificationError,
    claim_webhook_event,
    parse_webhook_event,
    verify_webhook_signature,
)

SECRET = "whsec_python_sdk"
NOW = 1_791_801_000
# Non-ASCII on purpose: the HMAC is over the raw bytes.
BODY = '{"id":"evt_1","type":"payment.captured","created":"2026-10-12T10:30:00Z","data":{"payment_id":"pay_1","description":"Čaj za 5 €"}}'.encode()


def header(t: int, body: bytes = BODY, secret: str = SECRET) -> str:
    return f"t={t},v1=" + hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()


def refusal(call: Callable[[], object]) -> str:
    try:
        call()
    except SignatureVerificationError as e:
        return e.code
    return "accepted"


def test_the_real_signature_verifies_and_parses_from_bytes_or_text() -> None:
    event = parse_webhook_event(BODY, header(NOW), SECRET, now=NOW)
    assert (event["id"], event["type"], event["data"]["description"]) == ("evt_1", "payment.captured", "Čaj za 5 €")
    assert parse_webhook_event(BODY.decode(), header(NOW), SECRET, now=NOW)["id"] == "evt_1"


@pytest.mark.parametrize(
    ("name", "call", "code"),
    [
        ("changed body", lambda: verify_webhook_signature(BODY + b" ", header(NOW), SECRET, now=NOW), "invalid_signature"),
        ("wrong secret", lambda: verify_webhook_signature(BODY, header(NOW), "whsec_other", now=NOW), "invalid_signature"),
        ("six minutes old", lambda: verify_webhook_signature(BODY, header(NOW - 360), SECRET, now=NOW), "expired_signature"),
        ("from the future", lambda: verify_webhook_signature(BODY, header(NOW + 360), SECRET, now=NOW), "expired_signature"),
        ("no header", lambda: verify_webhook_signature(BODY, None, SECRET, now=NOW), "missing_signature"),
        ("empty header", lambda: verify_webhook_signature(BODY, "", SECRET, now=NOW), "missing_signature"),
        ("garbage", lambda: verify_webhook_signature(BODY, "garbage", SECRET, now=NOW), "malformed_signature"),
        ("no v1", lambda: verify_webhook_signature(BODY, f"t={NOW}", SECRET, now=NOW), "malformed_signature"),
        ("timestamp not a number", lambda: verify_webhook_signature(BODY, "t=soon,v1=00", SECRET, now=NOW), "malformed_signature"),
        ("superscript digit", lambda: verify_webhook_signature(BODY, "t=²,v1=00", SECRET, now=NOW), "malformed_signature"),
        ("no secrets", lambda: verify_webhook_signature(BODY, header(NOW), [], now=NOW), "missing_secret"),
    ],
)
def test_everything_else_is_refused_with_its_reason(name: str, call: Callable[[], object], code: str) -> None:
    assert refusal(call) == code, name


def test_either_secret_verifies_during_a_rotation() -> None:
    assert refusal(lambda: verify_webhook_signature(BODY, header(NOW), ["whsec_new", SECRET], now=NOW)) == "accepted"
    assert refusal(lambda: verify_webhook_signature(BODY, header(NOW), ["whsec_new", "whsec_newer"], now=NOW)) == "invalid_signature"


def test_spaces_around_the_header_parts_are_tolerated() -> None:
    sig = header(NOW).split("v1=")[1]
    assert refusal(lambda: verify_webhook_signature(BODY, f"t={NOW}, v1={sig}", SECRET, now=NOW)) == "accepted"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"type":"payment.captured"}', b'{"id":1,"type":"x"}', b"\xff\xfe"])
def test_a_verified_body_that_is_not_an_event_is_refused(body: bytes) -> None:
    assert refusal(lambda: parse_webhook_event(body, header(NOW, body), SECRET, now=NOW)) == "malformed_event"


def test_a_claimed_event_is_handed_over_once_and_a_duplicate_is_none() -> None:
    store = MemoryWebhookEventStore()
    first = claim_webhook_event(BODY, header(NOW), SECRET, store, now=NOW)
    again = claim_webhook_event(BODY, header(NOW + 30), SECRET, store, now=NOW + 30)
    assert first is not None and first["id"] == "evt_1"
    assert again is None


def test_the_memory_store_forgets_the_oldest_when_full() -> None:
    store = MemoryWebhookEventStore(2)
    assert [store.claim(x) for x in ["a", "b", "c", "c", "a"]] == [True, True, True, False, True]


def test_the_default_clock_is_now() -> None:
    t = int(time.time())
    assert refusal(lambda: verify_webhook_signature(BODY, header(t), SECRET)) == "accepted"
