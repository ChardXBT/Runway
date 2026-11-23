"""Hosted-profile hardening: rate limits, quotas, kill switches and HTTP middleware.

Interfaces other code calls:

* `runway.hosted.quotas.require_send_allowed` before every platform-mutating
  send, `require_connected_account_slot` before connecting an account
* `runway.hosted.quotas.quotas_for`, `enforce_quota`, `publishing_allowed`,
  `publishing_gate`, `set_publishing_disabled`, `set_quota_overrides`
* `runway.hosted.ratelimit.RateLimiter` (protocol), `InProcessTokenBucket`,
  `RateLimit`, `enforce`
"""
