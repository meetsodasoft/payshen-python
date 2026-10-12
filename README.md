# Payshen Python

The Python client for the [Payshen](https://www.payshen.com) API: create and route payments, capture, void and
refund them, read their timeline, and verify the webhooks Payshen sends.

Payshen routes each payment to one of the providers you already have (Stripe, Adyen, Checkout.com, payabl. and
more). Payshen never holds or moves money: payments run on your own provider accounts.

- Python 3.9 or newer. No dependencies: the standard library only.
- Every POST carries an `Idempotency-Key`, so a retry after a dropped connection never charges twice.
- Retries with backoff and jitter, on the answers worth retrying and never on the ones the API says not to.
- Typed exceptions with the API's own error code and the request id support will ask for.

## Install

```bash
pip install payshen
```

## Create a payment

```python
import os
from payshen import Payshen

payshen = Payshen(os.environ["PAYSHEN_API_KEY"])  # ps_test_... in the sandbox

payment = payshen.payments.create(
    {
        "amount": 5000,              # minor units: EUR 50.00
        "currency": "EUR",
        "country": "DE",
        "reference": "order-1024",
        "capture_method": "manual",  # hold now, capture when you ship
        "return_url": "https://yourshop.com/checkout/return",
    },
    idempotency_key="order-1024",
)

# REQUIRES_ACTION: send the customer to the provider's page.
redirect_to = payment["next_action"]["url"]
```

The customer pays on the provider's page and comes back to your `return_url`. The return is not the outcome: the
outcome is the webhook (`payment.authorized`, `payment.captured`, `payment.declined`, ...).

## Capture, void, refund

```python
from payshen import PayshenError

payshen.payments.capture(payment_id, idempotency_key="capture-order-1024")               # in full
payshen.payments.capture(payment_id, amount=3000, idempotency_key="capture-order-1024")  # or in part
payshen.payments.void(payment_id, idempotency_key="void-order-1024")                     # before capture

try:
    payshen.payments.refund(payment_id, amount=2000, idempotency_key="refund-order-1024-1")
except PayshenError as e:
    # 409 refund_pending: accepted, not failed. The provider confirms a moment later and
    # payment.refunded or payment.refund_failed follows. Do not send it again under a new key.
    if e.code != "refund_pending":
        raise
```

A capture or void a provider only accepted answers with the payment `PENDING`; it becomes `CAPTURED` or
`CANCELLED` when the provider confirms, and the webhook follows.

## Read

```python
payment = payshen.payments.get(payment_id)
timeline = payshen.payments.events(payment_id)

page = payshen.payments.list(limit=50, status="captured")
for payment in payshen.payments.all(status="captured"):
    ...  # every page, newest first

for event in payshen.events.all(type="payment.captured"):
    ...  # the same events webhooks deliver, for polling
```

## Webhooks

```python
from payshen import SignatureVerificationError, parse_webhook_event

# Flask. Django: request.body and request.headers["X-Payshen-Signature"].
raw = request.get_data()  # the RAW body: a body re-encoded from json never verifies
try:
    event = parse_webhook_event(raw, request.headers.get("X-Payshen-Signature"), os.environ["PAYSHEN_WEBHOOK_SECRET"])
except SignatureVerificationError:
    return "", 400

# Deliveries are retried, so the same event can arrive twice. Record event["id"]
# and skip one you have already processed.
if event["type"] == "payment.captured":
    fulfil(event["data"]["reference"])
return "", 204
```

The signature proves a delivery came from Payshen and is recent (5 minutes by default); it does not make it new.
`claim_webhook_event(raw, header, secret, store)` verifies, parses and takes each event once, with a store you back
with your database:

```python
class DbEventStore:
    def claim(self, event_id: str) -> bool:
        # PostgreSQL; MySQL: INSERT IGNORE and the affected row count
        cur.execute("INSERT INTO processed_webhook_events (id) VALUES (%s) ON CONFLICT (id) DO NOTHING RETURNING id", (event_id,))
        return cur.fetchone() is not None
```

During a secret rotation pass both: `parse_webhook_event(raw, header, [new_secret, old_secret])`.

## Errors

| Exception | When |
|---|---|
| `AuthenticationError` | 401 or 403: a missing, wrong or revoked key, or one not allowed this call |
| `ValidationError` | 422: refused as sent; `code` names the rule |
| `RateLimitError` | 429 after the client's retries; `retry_after_seconds` when the API said |
| `NetworkError` | No answer at all. On a POST, retry with the same idempotency key |
| `PayshenError` | Everything else, and the parent of all of the above |

Every exception has `status`, `code` (`refund_pending`, `not_capturable`, `payment_result_unknown`, ...) and
`request_id`. Branch on `code`, not on the message.

## Options

```python
Payshen(key, timeout=20.0, max_retries=3, base_url="https://www.payshen.com")
```

`payshen.request("GET", "/api/v1/...")` calls any endpoint and returns the whole answer.

## Documentation

- API reference and quickstart: <https://www.payshen.com/developers>
- OpenAPI 3.1: <https://www.payshen.com/api/v1/openapi.json>

## License

MIT
