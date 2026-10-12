"""The client against a recorded transport: what it sends, how it retries, and
what it raises, the same rules as the TypeScript SDK."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Union

import pytest

import payshen
from payshen import (
    AuthenticationError,
    NetworkError,
    Payshen,
    PayshenError,
    RateLimitError,
    Response,
    TransportError,
    ValidationError,
)

PAYMENT = {"payment_id": "pay_1", "status": "REQUIRES_ACTION", "next_action": {"type": "REDIRECT", "url": "https://pay.example/next"}}


def answer(status: int, body: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, b"" if body is None else json.dumps(body).encode())


class FakeTransport:
    def __init__(self, *answers: Union[Response, TransportError]) -> None:
        self.answers = list(answers)
        self.sent: List[Dict[str, Any]] = []

    def send(self, method, url, headers, body, timeout):  # type: ignore[no-untyped-def]
        self.sent.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        nxt = self.answers.pop(0)
        if isinstance(nxt, TransportError):
            raise nxt
        return nxt


@pytest.fixture
def slept() -> List[float]:
    return []


def client(t: FakeTransport, slept: List[float], **options: Any) -> Payshen:
    return Payshen("ps_test_abc", transport=t, sleep=slept.append, **options)


def body_of(sent: Dict[str, Any]) -> Any:
    return json.loads(sent["body"]) if sent["body"] is not None else None


def test_create_sends_the_request_the_api_documents(slept: List[float]) -> None:
    t = FakeTransport(answer(201, {"data": PAYMENT}))
    payment = client(t, slept).payments.create(
        {"amount": 5000, "currency": "EUR", "country": "DE", "reference": "order-1024", "return_url": "https://shop.example/return"},
        idempotency_key="order-1024",
    )
    assert payment["next_action"]["url"] == "https://pay.example/next"
    sent = t.sent[0]
    assert sent["method"] == "POST"
    assert sent["url"] == "https://www.payshen.com/api/v1/payments"
    assert sent["headers"]["Authorization"] == "Bearer ps_test_abc"
    assert sent["headers"]["Idempotency-Key"] == "order-1024"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["headers"]["User-Agent"] == f"payshen-python/{payshen.VERSION}"
    assert body_of(sent) == {"amount": 5000, "currency": "EUR", "country": "DE", "reference": "order-1024", "return_url": "https://shop.example/return"}
    assert slept == []


def test_every_post_carries_a_key_even_when_the_caller_gave_none(slept: List[float]) -> None:
    t = FakeTransport(answer(201, {"data": PAYMENT}), answer(200, {"data": PAYMENT}))
    c = client(t, slept)
    c.payments.create({"amount": 100})
    c.payments.get("pay_1")
    assert re.fullmatch(r"sdk_[0-9a-f]{32}", t.sent[0]["headers"]["Idempotency-Key"])
    assert "Idempotency-Key" not in t.sent[1]["headers"]
    assert t.sent[1]["body"] is None
    assert "Content-Type" not in t.sent[1]["headers"]


def test_capture_void_and_refund_send_objects(slept: List[float]) -> None:
    t = FakeTransport(answer(200, {"data": {"status": "CAPTURED"}}), answer(200, {"data": {"status": "CANCELLED"}}), answer(200, {"data": {"status": "PARTIALLY_REFUNDED"}}))
    c = client(t, slept)
    c.payments.capture("pay_1")
    c.payments.void("pay_2")
    c.payments.refund("pay_3", amount=2000, idempotency_key="refund-1")
    assert [s["body"] for s in t.sent] == [b"{}", b"{}", b'{"amount":2000}']
    assert [s["url"].rsplit("/api/v1", 1)[1] for s in t.sent] == ["/payments/pay_1/capture", "/payments/pay_2/void", "/payments/pay_3/refunds"]
    assert t.sent[2]["headers"]["Idempotency-Key"] == "refund-1"


def test_ids_are_encoded_into_the_path(slept: List[float]) -> None:
    t = FakeTransport(answer(200, {"data": PAYMENT}))
    client(t, slept).payments.get("a/b?c")
    assert t.sent[0]["url"] == "https://www.payshen.com/api/v1/payments/a%2Fb%3Fc"


def test_a_retry_reuses_the_same_key_so_it_cannot_charge_twice(slept: List[float]) -> None:
    t = FakeTransport(TransportError("connection reset"), answer(503, {"error": {"code": "internal_error", "message": "busy"}}), answer(201, {"data": PAYMENT}))
    payment = client(t, slept).payments.create({"amount": 100})
    assert payment["payment_id"] == "pay_1"
    assert len(t.sent) == 3
    assert len({s["headers"]["Idempotency-Key"] for s in t.sent}) == 1
    assert len(slept) == 2
    assert slept[0] >= 0.25 and max(slept) <= 4.0


def test_an_outcome_the_api_says_not_to_retry_is_not_retried(slept: List[float]) -> None:
    t = FakeTransport(answer(502, {"error": {"code": "payment_result_unknown", "message": "The provider did not answer."}}, {"X-Request-Id": "req_9"}))
    with pytest.raises(PayshenError) as e:
        client(t, slept).payments.create({"amount": 100})
    assert (e.value.status, e.value.code, e.value.request_id) == (502, "payment_result_unknown", "req_9")
    assert len(t.sent) == 1


def test_refund_pending_is_raised_with_its_code_and_not_retried(slept: List[float]) -> None:
    t = FakeTransport(answer(409, {"error": {"code": "refund_pending", "message": "Accepted, not refunded yet."}}))
    with pytest.raises(PayshenError) as e:
        client(t, slept).payments.refund("pay_1", amount=2000)
    assert (e.value.status, e.value.code) == (409, "refund_pending")
    assert len(t.sent) == 1


def test_rate_limit_honours_retry_after_capped_then_raises(slept: List[float]) -> None:
    limited = answer(429, {"error": {"code": "rate_limited", "message": "Slow down."}}, {"Retry-After": "30"})
    t = FakeTransport(limited, limited, limited, limited)
    with pytest.raises(RateLimitError) as e:
        client(t, slept).payments.list()
    assert e.value.retry_after_seconds == 30
    assert len(t.sent) == 4
    assert slept == [4.0, 4.0, 4.0]


def test_auth_and_validation_errors_are_typed_and_not_retried(slept: List[float]) -> None:
    t = FakeTransport(
        answer(401, {"error": {"code": "invalid_api_key", "message": "Invalid API key."}}),
        answer(422, {"error": {"code": "invalid_amount", "message": "amount must be 1..500."}}),
    )
    c = client(t, slept)
    with pytest.raises(AuthenticationError) as auth:
        c.payments.get("pay_1")
    assert auth.value.code == "invalid_api_key"
    with pytest.raises(ValidationError) as invalid:
        c.payments.refund("pay_1", amount=600)
    assert (invalid.value.code, str(invalid.value)) == ("invalid_amount", "amount must be 1..500.")
    assert len(t.sent) == 2 and slept == []


def test_routing_failed_carries_why_each_connection_was_left_out(slept: List[float]) -> None:
    excluded = [{"connection": "Stripe EU", "reason": "currency_not_supported"}]
    t = FakeTransport(answer(422, {"error": {"code": "routing_failed", "message": "No connection can take it.", "excluded": excluded}}))
    with pytest.raises(PayshenError) as e:
        client(t, slept).payments.create({"amount": 100})
    assert e.value.excluded == excluded


def test_no_answer_at_all_becomes_a_network_error_after_the_retries(slept: List[float]) -> None:
    t = FakeTransport(*(TransportError("timeout") for _ in range(4)))
    with pytest.raises(NetworkError):
        client(t, slept).payments.get("pay_1")
    assert len(t.sent) == 4


def test_an_error_without_a_body_still_says_what_happened(slept: List[float]) -> None:
    t = FakeTransport(Response(404, {}, b""))
    with pytest.raises(PayshenError) as e:
        client(t, slept).payments.get("missing")
    assert (e.value.status, e.value.code, str(e.value)) == (404, "unknown_error", "Request failed with status 404.")


def test_all_walks_every_page_by_the_cursor_beside_data(slept: List[float]) -> None:
    t = FakeTransport(
        answer(200, {"data": [{"payment_id": "p3"}, {"payment_id": "p2"}], "next_cursor": "c2", "has_more": True}),
        answer(200, {"data": [{"payment_id": "p1"}], "next_cursor": None, "has_more": False}),
    )
    ids = [p["payment_id"] for p in client(t, slept).payments.all(limit=2, status="captured")]
    assert ids == ["p3", "p2", "p1"]
    assert t.sent[0]["url"] == "https://www.payshen.com/api/v1/payments?limit=2&status=captured"
    assert t.sent[1]["url"] == "https://www.payshen.com/api/v1/payments?limit=2&before=c2&status=captured"


def test_suggestion_states_are_sent_comma_separated(slept: List[float]) -> None:
    t = FakeTransport(answer(200, {"data": []}))
    page = client(t, slept).suggestions.list(state=["open", "snoozed"])
    assert page.data == [] and page.has_more is False
    assert t.sent[0]["url"] == "https://www.payshen.com/api/v1/suggestions?state=open%2Csnoozed"


def test_the_key_is_never_sent_over_plain_http() -> None:
    with pytest.raises(ValueError):
        Payshen("ps_live_x", base_url="http://api.example.com")


def test_localhost_over_http_is_allowed_for_a_mock_server(slept: List[float]) -> None:
    t = FakeTransport(answer(200, {"data": PAYMENT}))
    client(t, slept, base_url="http://127.0.0.1:4010/").payments.get("pay_1")
    assert t.sent[0]["url"] == "http://127.0.0.1:4010/api/v1/payments/pay_1"


def test_a_key_that_is_not_a_payshen_key_is_refused_at_once() -> None:
    with pytest.raises(ValueError):
        Payshen("sk_live_123")


def test_the_default_transport_does_not_follow_a_redirect() -> None:
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(308)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = Payshen("ps_test_abc", base_url=f"http://127.0.0.1:{server.server_address[1]}", max_retries=0)
        with pytest.raises(PayshenError) as e:
            c.payments.create({"amount": 100})
        assert e.value.status == 308
    finally:
        server.shutdown()
        server.server_close()
