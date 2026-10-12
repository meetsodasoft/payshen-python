"""Transports carry bytes; the client decides retries, idempotency and errors."""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

try:  # Python 3.8+: typing.Protocol
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment,misc]

__all__ = ["Response", "Transport", "TransportError", "UrllibTransport"]


@dataclass(frozen=True)
class Response:
    status: int
    #: lowercased name -> value
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


class TransportError(Exception):
    """No answer came at all: the client retries it, then raises ``NetworkError``."""


class Transport(Protocol):
    def send(
        self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float
    ) -> Response:  # pragma: no cover - interface
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect on an API call is a misconfigured base URL. Following it would
    # turn a POST into a GET, or send the key somewhere else.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibTransport:
    """The default transport: the standard library, TLS verified, no redirects followed."""

    def __init__(self, ssl_context: Optional[ssl.SSLContext] = None) -> None:
        context = ssl_context or ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def send(
        self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as answer:
                return Response(answer.status, _headers(answer.headers), answer.read())
        except urllib.error.HTTPError as answer:
            # A non-2xx answer is still an answer: the client reads its body.
            try:
                payload = answer.read()
            finally:
                answer.close()
            return Response(answer.code, _headers(answer.headers), payload)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            raise TransportError(f"Request to Payshen failed: {e}") from e


def _headers(message) -> Dict[str, str]:  # type: ignore[no-untyped-def]
    if message is None:
        return {}
    return {k.lower(): v for k, v in message.items()}
