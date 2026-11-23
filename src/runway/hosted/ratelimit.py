"""Request rate limiting for the hosted profile (spec 05 section 6).

`RateLimiter` is the seam: the in-process token bucket below serves one API
process; a shared store (Redis, Postgres) can implement the same protocol
when the API runs as more than one process.

Keys: a signed-in request is limited per user (`user:<provider>:<id>`) and per
workspace; only requests without a valid session spend the per-address bucket
(`ip:<address>`), so users behind one proxy hop or one NAT do not share a
budget. `client_key` folds an IPv6 address to its /64, the block one
subscriber usually holds, so one client cannot mint fresh buckets.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from runway.auth.problems import Problem


@dataclass(frozen=True, slots=True)
class RateLimit:
    """`requests` per `per_seconds`, with a burst of up to `requests`."""

    requests: int
    per_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests < 1 or self.per_seconds <= 0:
            raise ValueError("a rate limit needs at least one request per positive period")

    @property
    def refill_per_second(self) -> float:
        return self.requests / self.per_seconds


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: float = 0.0


class RateLimiter(Protocol):
    def consume(self, key: str, limit: RateLimit, cost: float = 1.0) -> RateDecision:
        """Spend `cost` tokens from `key`'s bucket, or say how long to wait."""
        ...


class InProcessTokenBucket:
    """Thread-safe token buckets held in memory, least recently used evicted first."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, key: str, limit: RateLimit, cost: float = 1.0) -> RateDecision:
        with self._lock:
            now = self._clock()
            tokens, updated = self._buckets.pop(key, (float(limit.requests), now))
            tokens = min(float(limit.requests), tokens + (now - updated) * limit.refill_per_second)
            if tokens >= cost:
                self._store(key, tokens - cost, now)
                return RateDecision(allowed=True)
            self._store(key, tokens, now)
            return RateDecision(
                allowed=False, retry_after_seconds=(cost - tokens) / limit.refill_per_second
            )

    def _store(self, key: str, tokens: float, now: float) -> None:
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)


IPV6_CLIENT_PREFIX = 64


def client_key(address: str | None) -> str:
    """The per-address bucket key: IPv4 as is, IPv6 folded to its /64."""
    if not address:
        return "ip:unknown"
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return f"ip:{address}"
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return f"ip:{parsed.ipv4_mapped}"
        network = ipaddress.IPv6Network((parsed, IPV6_CLIENT_PREFIX), strict=False)
        return f"ip:{network}"
    return f"ip:{parsed}"


def rate_limited(decision: RateDecision, detail: str) -> Problem:
    retry_after = max(1, math.ceil(decision.retry_after_seconds))
    return Problem(
        429,
        "rate_limited",
        "Too many requests",
        detail,
        headers={"Retry-After": str(retry_after)},
    )


def enforce(limiter: RateLimiter, key: str, limit: RateLimit, detail: str) -> None:
    """Raise a 429 `rate_limited` problem when `key` is over `limit`."""
    decision = limiter.consume(key, limit)
    if not decision.allowed:
        raise rate_limited(decision, detail)
