"""Against the real API over the real transport, with a sandbox key.

Skipped unless PAYSHEN_TEST_KEY (a ps_test_ key) is set; PAYSHEN_BASE_URL
points it at another deployment. Nothing here moves money: a test key only
reaches provider sandboxes, and the payment it makes is left on its page.
"""

from __future__ import annotations

import os
import secrets

import pytest

from payshen import AuthenticationError, Payshen, PayshenError

KEY = os.environ.get("PAYSHEN_TEST_KEY", "")
BASE = os.environ.get("PAYSHEN_BASE_URL") or "https://www.payshen.com"
pytestmark = pytest.mark.skipif(not KEY.startswith("ps_test_"), reason="PAYSHEN_TEST_KEY (a ps_test_ key) is not set")


def test_a_hosted_payment_from_create_to_its_timeline() -> None:
    client = Payshen(KEY, base_url=BASE)
    reference = f"python-sdk-{secrets.token_hex(4)}"
    params = {"amount": 1250, "currency": "EUR", "country": "DE", "method": "card", "reference": reference, "return_url": "https://example.com/payshen-return"}

    payment = client.payments.create(params, idempotency_key=reference)
    assert payment["status"] == "REQUIRES_ACTION"
    assert payment["next_action"]["url"].startswith("https://")
    pid = payment.get("payment_id") or payment["id"]

    again = client.payments.create(params, idempotency_key=reference)
    assert (again.get("payment_id") or again["id"]) == pid, "the same key answers the same payment"

    read = client.payments.get(pid)
    assert (read["reference"], read["amount"]) == (reference, 1250)
    assert "payment.created" in [e["type"] for e in client.payments.events(pid)]
    assert pid in [p.get("payment_id") or p["id"] for p in client.payments.list(limit=5).data]

    with pytest.raises(PayshenError) as e:
        client.payments.capture(pid)
    assert (e.value.status, e.value.code) == (409, "not_capturable")
    assert e.value.request_id


def test_a_wrong_key_is_an_authentication_error() -> None:
    with pytest.raises(AuthenticationError):
        Payshen("ps_test_not_a_real_key", base_url=BASE).payments.list()
