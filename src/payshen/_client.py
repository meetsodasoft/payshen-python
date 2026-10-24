"""The Payshen API client."""

from __future__ import annotations

import json
import random
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlencode

from ._http import Response, Transport, TransportError, UrllibTransport
from ._version import VERSION
from .errors import AuthenticationError, NetworkError, PayshenError, RateLimitError, ValidationError

__all__ = ["Payshen", "Page"]

#: HTTP statuses worth another attempt, as in the TypeScript SDK.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Error codes a retry cannot improve although their status is retryable.
_FINAL_CODES = frozenset(
    {
        "payment_result_unknown",
        "state_unconfirmed",
        "state_conflict",
        "capture_result_unknown",
        "void_result_unknown",
        "refund_result_unknown",
        "psp_error",
        "provider_unavailable",
        "merchant_limit",
    }
)
_BACKOFF_BASE_MS = 250
_BACKOFF_CAP_MS = 4000
_LOCALHOST = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)", re.IGNORECASE)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

Json = Dict[str, Any]


@dataclass(frozen=True)
class Page:
    """One page of a list, newest first. Pass ``next_cursor`` as ``before`` for the next page."""

    data: List[Json]
    next_cursor: Optional[str]
    has_more: bool

    @classmethod
    def from_answer(cls, answer: Json) -> "Page":
        data = answer.get("data")
        meta = answer.get("meta") if isinstance(answer.get("meta"), dict) else {}
        cursor = answer.get("next_cursor", meta.get("next_cursor"))
        cursor = cursor if isinstance(cursor, str) and cursor else None
        has_more = answer.get("has_more")
        return cls(
            data=list(data) if isinstance(data, list) else [],
            next_cursor=cursor,
            has_more=has_more if isinstance(has_more, bool) else cursor is not None,
        )


def _query(params: Mapping[str, Any]) -> str:
    pairs = []
    for name, value in params.items():
        if value is None or value == "" or value == [] or value == ():
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        pairs.append((name, str(value)))
    return "?" + urlencode(pairs, quote_via=quote) if pairs else ""


def _seg(value: str, name: str = "id") -> str:
    # Encoding alone is not enough: "." and ".." survive it, and get(".") would
    # read the list while refund("..") would post to another route. Payshen ids
    # are letters, digits, "_" and "-".
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'{name} must be 1-128 letters, digits, "_" or "-".')
    return value


class Payshen:
    """The Payshen API client.

    >>> payshen = Payshen("ps_test_...")
    >>> payment = payshen.payments.create({...}, idempotency_key="order-1024")

    Every POST carries an ``Idempotency-Key`` (yours, or one generated per call
    and reused across this client's own retries), so a retry after a dropped
    connection never charges twice. Answers are the API's JSON as dicts.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://www.payshen.com",
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: Optional[Transport] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not api_key or not api_key.startswith("ps_"):
            raise ValueError("A Payshen API key (ps_test_... or ps_live_...) is required.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        # The key travels in a header on every request: over plain HTTP anything
        # on the path can read it. Localhost is exempt, for a mock server.
        if self._base_url.lower().startswith("http://") and not _LOCALHOST.match(self._base_url):
            raise ValueError("Payshen base_url must use https: an API key sent over plain HTTP is readable in transit.")
        # Any other scheme (ftp://, file://...) is never the API, and urllib would open it.
        if not self._base_url.lower().startswith(("https://", "http://")):
            raise ValueError("Payshen base_url must be an https URL.")
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._transport: Transport = transport or UrllibTransport()
        self._sleep = sleep or time.sleep

        self.payments = Payments(self)
        self.events = Events(self)
        self.insights = Insights(self)
        self.suggestions = Suggestions(self)
        self.merchants = Merchants(self)

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Json:
        """Call any endpoint and get the whole answer (``data``, and ``next_cursor``,
        ``summary`` or ``result`` where the endpoint sends them)."""
        method = method.upper()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": f"payshen-python/{VERSION}",
        }
        encoded: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # Without a key, a retry after a connection dropped AFTER the server
        # created the payment would create a second one. The caller's key wins:
        # only the caller knows whether two calls are the same operation.
        if method == "POST":
            headers["Idempotency-Key"] = idempotency_key or f"sdk_{secrets.token_hex(16)}"
        elif idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        url = self._base_url + path
        attempt = 0
        while True:
            try:
                response = self._transport.send(method, url, headers, encoded, self._timeout)
            except TransportError as e:
                if attempt < self._max_retries:
                    self._wait(attempt)
                    attempt += 1
                    continue
                raise NetworkError(str(e)) from e

            payload = _decode(response)
            if 200 <= response.status < 300:
                return payload or {}

            error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else None
            message = error.get("message") if isinstance(error.get("message"), str) else None
            retryable = response.status in _RETRY_STATUSES and code not in _FINAL_CODES

            if response.status == 429:
                retry_after = _retry_after(response)
                if retryable and attempt < self._max_retries:
                    self._wait(attempt, retry_after)
                    attempt += 1
                    continue
                raise _fail(RateLimitError(code or "rate_limited", message or "Rate limit exceeded.", retry_after), response, error)
            if retryable and attempt < self._max_retries:
                self._wait(attempt)
                attempt += 1
                continue
            if response.status in (401, 403):
                raise _fail(AuthenticationError(response.status, code or "unauthorized", message or "Request not authorised."), response, error)
            if response.status == 422:
                raise _fail(ValidationError(422, code or "validation_error", message or "Validation error."), response, error)
            raise _fail(
                PayshenError(response.status, code or "unknown_error", message or f"Request failed with status {response.status}."),
                response,
                error,
            )

    def _wait(self, attempt: int, retry_after_seconds: Optional[int] = None) -> None:
        """Exponential with jitter; a server's Retry-After capped like any other wait."""
        if retry_after_seconds is not None and retry_after_seconds > 0:
            ms = min(_BACKOFF_CAP_MS, retry_after_seconds * 1000)
        else:
            exp = min(_BACKOFF_CAP_MS, _BACKOFF_BASE_MS * (2**attempt))
            ms = exp + random.random() * exp * 0.3
        self._sleep(ms / 1000)


def _decode(response: Response) -> Optional[Json]:
    if not response.body:
        return None
    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _retry_after(response: Response) -> Optional[int]:
    value = response.header("retry-after")
    return int(value) if value is not None and value.isascii() and value.isdigit() else None


def _fail(e: PayshenError, response: Response, error: Mapping[str, Any]) -> PayshenError:
    """Every error leaves carrying the id support will ask for."""
    e.request_id = response.header("x-request-id")
    if isinstance(error.get("excluded"), list):
        e.excluded = list(error["excluded"])
    if isinstance(error.get("refund_id"), str):
        e.refund_id = error["refund_id"]
    return e


def _data(answer: Json) -> Json:
    data = answer.get("data")
    return data if isinstance(data, dict) else {}


def _items(answer: Json) -> List[Json]:
    data = answer.get("data")
    return list(data) if isinstance(data, list) else []


class Payments:
    """Payments: create, read, list, and the operations on one.

    Pass your own ``idempotency_key`` on a POST (your order id,
    ``capture-<order>``, ``refund-<order>-1``) to make a retry across processes
    safe; the client generates one per call otherwise.
    """

    def __init__(self, client: Payshen) -> None:
        self._client = client

    def create(self, params: Mapping[str, Any], *, idempotency_key: Optional[str] = None) -> Json:
        """Create and route a payment. A hosted payment answers ``REQUIRES_ACTION``
        with ``next_action.url``: send the customer there."""
        return _data(self._client.request("POST", "/api/v1/payments", params, idempotency_key=idempotency_key))

    def get(self, payment_id: str) -> Json:
        return _data(self._client.request("GET", f"/api/v1/payments/{_seg(payment_id, 'payment_id')}"))

    def list(self, *, limit: Optional[int] = None, before: Optional[str] = None, status: Optional[str] = None) -> Page:
        """One page, newest first, in the key's environment."""
        return Page.from_answer(self._client.request("GET", "/api/v1/payments" + _query({"limit": limit, "before": before, "status": status})))

    def all(self, *, limit: Optional[int] = None, status: Optional[str] = None, before: Optional[str] = None) -> Iterator[Json]:
        """Every payment, page by page, as one sequence."""
        while True:
            page = self.list(limit=limit, before=before, status=status)
            yield from page.data
            if not (page.has_more and page.next_cursor):
                return
            before = page.next_cursor

    def events(self, payment_id: str) -> List[Json]:
        """The payment's timeline: created, authorized, captured, refunded, ..."""
        return _items(self._client.request("GET", f"/api/v1/payments/{_seg(payment_id, 'payment_id')}/events"))

    def capture(self, payment_id: str, *, amount: Optional[int] = None, idempotency_key: Optional[str] = None) -> Json:
        """Capture an authorization, in full (no amount) or in part. A provider that only
        accepts it first answers with the payment ``PENDING``; ``payment.captured`` follows."""
        body = {} if amount is None else {"amount": amount}
        return _data(self._client.request("POST", f"/api/v1/payments/{_seg(payment_id, 'payment_id')}/capture", body, idempotency_key=idempotency_key))

    def void(self, payment_id: str, *, idempotency_key: Optional[str] = None) -> Json:
        """Release an authorization before capture."""
        return _data(self._client.request("POST", f"/api/v1/payments/{_seg(payment_id, 'payment_id')}/void", {}, idempotency_key=idempotency_key))

    def refund(self, payment_id: str, *, amount: Optional[int] = None, idempotency_key: Optional[str] = None) -> Json:
        """Refund a captured payment, in full (no amount) or in part.

        Most providers confirm a refund a moment later: the API then answers
        ``409 refund_pending``, raised here as a ``PayshenError`` with that
        ``code``. It is accepted, not failed. Do not send it again under a new
        key: ``payment.refunded`` or ``payment.refund_failed`` follows, and the
        same key answers the final result once it is settled.
        """
        body = {} if amount is None else {"amount": amount}
        return _data(self._client.request("POST", f"/api/v1/payments/{_seg(payment_id, 'payment_id')}/refunds", body, idempotency_key=idempotency_key))


class Events:
    """The organization's event feed, newest first: the events webhooks deliver, for polling."""

    def __init__(self, client: Payshen) -> None:
        self._client = client

    def list(self, *, limit: Optional[int] = None, before: Optional[str] = None, type: Optional[str] = None) -> Page:  # noqa: A002
        return Page.from_answer(self._client.request("GET", "/api/v1/events" + _query({"limit": limit, "before": before, "type": type})))

    def all(self, *, limit: Optional[int] = None, type: Optional[str] = None, before: Optional[str] = None) -> Iterator[Json]:  # noqa: A002
        while True:
            page = self.list(limit=limit, before=before, type=type)
            yield from page.data
            if not (page.has_more and page.next_cursor):
                return
            before = page.next_cursor


class Insights:
    """What to change next, ranked by the money it is about. Rate limited to 20 calls a minute."""

    def __init__(self, client: Payshen) -> None:
        self._client = client

    def list(self, *, range: Optional[str] = None) -> Json:  # noqa: A002
        """``{"data": [...], "summary": {...}}``"""
        answer = self._client.request("GET", "/api/v1/insights" + _query({"range": range}))
        summary = answer.get("summary")
        return {"data": _items(answer), "summary": summary if isinstance(summary, dict) else None}


class Suggestions:
    """The routing engine's suggestions. Accepting applies one the safe way, never repointing live traffic at once."""

    def __init__(self, client: Payshen) -> None:
        self._client = client

    def list(
        self,
        *,
        state: Union[str, Sequence[str], None] = None,
        limit: Optional[int] = None,
        before: Optional[str] = None,
    ) -> Page:
        states = [state] if isinstance(state, str) else state
        return Page.from_answer(self._client.request("GET", "/api/v1/suggestions" + _query({"state": states, "limit": limit, "before": before})))

    def get(self, suggestion_id: str) -> Json:
        return _data(self._client.request("GET", f"/api/v1/suggestions/{_seg(suggestion_id, 'suggestion_id')}"))

    def accept(self, suggestion_id: str, *, idempotency_key: Optional[str] = None) -> Json:
        """``{"data": {...}, "result": {...}}``. A decided suggestion answers 409 ``suggestion_not_open``."""
        answer = self._client.request("POST", f"/api/v1/suggestions/{_seg(suggestion_id, 'suggestion_id')}/accept", {}, idempotency_key=idempotency_key)
        result = answer.get("result")
        return {"data": _data(answer), "result": result if isinstance(result, dict) else None}

    def reject(self, suggestion_id: str, *, reason: Optional[str] = None, idempotency_key: Optional[str] = None) -> Json:
        body = {} if reason is None else {"reason": reason}
        return _data(self._client.request("POST", f"/api/v1/suggestions/{_seg(suggestion_id, 'suggestion_id')}/reject", body, idempotency_key=idempotency_key))

    def snooze(self, suggestion_id: str, *, idempotency_key: Optional[str] = None) -> Json:
        """Hide it for 7 days; it comes back open if the evidence still holds."""
        return _data(self._client.request("POST", f"/api/v1/suggestions/{_seg(suggestion_id, 'suggestion_id')}/snooze", {}, idempotency_key=idempotency_key))


class Merchants:
    """White-label partners only. Both calls answer 403 ``not_a_partner`` otherwise; provisioning needs a live key."""

    def __init__(self, client: Payshen) -> None:
        self._client = client

    def list(self) -> List[Json]:
        return _items(self._client.request("GET", "/api/v1/merchants"))

    def create(self, params: Mapping[str, Any], *, idempotency_key: Optional[str] = None) -> Json:
        """Provision a merchant and invite its owner. A pending invitation answers 409 ``already_invited``."""
        return _data(self._client.request("POST", "/api/v1/merchants", params, idempotency_key=idempotency_key))
