# Changelog

## 0.1.1

2026-10-14. Additive; nothing you use changes.

- `PayshenError.refund_id`: a refund answered `refund_pending`, `refund_result_unknown`, `psp_error` or
  `state_unconfirmed` names the refund it is about, for the `payment.refunded` or `payment.refund_failed` webhook
  that settles it. A successful refund's answer now carries `refund` (`id`, `amount`, `status`) beside the payment's
  figures; it comes through in the returned dict.

## 0.1.0

The first release, with the same rules as the TypeScript SDK (`@payshen/sdk` 0.9.3).

- Payments: create, get, list, `all()` over every page, events, capture (full or partial), void, refund (full or
  partial).
- Events feed, insights, suggestions (list, get, accept, reject, snooze), partner merchants.
- An `Idempotency-Key` on every POST, yours or generated and reused across retries.
- Retries on 408, 425, 429, 500, 502, 503 and 504 with backoff and jitter, never on an outcome the API says not to
  retry (`payment_result_unknown`, `*_result_unknown`, `state_unconfirmed`, `state_conflict`, `psp_error`,
  `provider_unavailable`, `merchant_limit`). `Retry-After` is honoured, capped at 4 seconds.
- Webhooks: `verify_webhook_signature`, `parse_webhook_event`, `claim_webhook_event` with a store, and secret
  rotation.
- HTTPS only, except localhost; redirects are never followed. No dependencies.
