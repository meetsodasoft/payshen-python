# Changelog

## 0.1.2

2026-10-17. Security fixes; upgrade.

- **Webhook verification never accepts an empty secret.** `verify_webhook_signature` and `parse_webhook_event`
  ignore an empty or non-string secret, and refuse with `missing_secret` when none is left. Before, an empty value
  (for example `os.environ.get("PAYSHEN_WEBHOOK_SECRET", "")`, or an empty old secret in a rotation list) let
  anyone sign a forged event with an empty key.
- **A hostile header is `malformed_signature`**, not a `TypeError` or `ValueError`: a non-ASCII `v1` or an
  over-long `t` used to escape the documented `except SignatureVerificationError`.
- **`base_url` must be an `https://` URL** (`http://` for localhost only); other schemes are refused.

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
