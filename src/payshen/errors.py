"""Exceptions. Branch on ``code`` (the API's own error code), not on the message."""

from __future__ import annotations

from typing import Any, List, Optional

__all__ = [
    "PayshenError",
    "AuthenticationError",
    "SignatureVerificationError",
    "ValidationError",
    "RateLimitError",
    "NetworkError",
]


class PayshenError(Exception):
    """An answer that is not a success, or a request that never got one.

    ``status`` is the HTTP status (0 when no answer came), ``code`` the API's
    error code (``refund_pending``, ``not_capturable``, ``invalid_api_key``,
    ...). Quote ``request_id`` in a support conversation.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        #: The ``X-Request-Id`` of the failed call, when the API returned one.
        self.request_id: Optional[str] = None
        #: On ``routing_failed``: every connection considered and why it was left out.
        self.excluded: Optional[List[Any]] = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, code={self.code!r}, message={self.message!r})"


class AuthenticationError(PayshenError):
    """401 or 403: the key is missing, wrong, revoked, or not allowed this call."""


class SignatureVerificationError(AuthenticationError):
    """A webhook delivery that did not verify. Answer it 400 and do not act on it.

    ``code`` is ``missing_signature``, ``malformed_signature``,
    ``expired_signature``, ``missing_secret``, ``invalid_signature``, or
    ``malformed_event`` for a verified body that is not a Payshen event.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(400, code, message)


class ValidationError(PayshenError):
    """422: the request was understood and refused. ``code`` names the rule."""


class RateLimitError(PayshenError):
    """429 after the client's own retries. Wait ``retry_after_seconds`` when the API said."""

    def __init__(self, code: str, message: str, retry_after_seconds: Optional[int]) -> None:
        super().__init__(429, code, message)
        self.retry_after_seconds = retry_after_seconds


class NetworkError(PayshenError):
    """No answer: a timeout, a refused connection, TLS. On a POST, retry with the SAME idempotency key."""

    def __init__(self, message: str) -> None:
        super().__init__(0, "network_error", message)
