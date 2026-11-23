from __future__ import annotations

import pytest

from runway.auth.problems import Problem
from runway.hosted.quotas import UNLIMITED, WorkspaceQuotas, enforce_quota
from runway.hosted.ratelimit import InProcessTokenBucket, RateLimit, client_key, enforce


def test_token_bucket_allows_a_burst_then_refills_over_time() -> None:
    clock = [0.0]
    limiter = InProcessTokenBucket(clock=lambda: clock[0])
    limit = RateLimit(3, per_seconds=60)
    assert [limiter.consume("k", limit).allowed for _ in range(4)] == [True, True, True, False]
    refused = limiter.consume("k", limit)
    assert refused.retry_after_seconds == pytest.approx(20.0)
    assert limiter.consume("other", limit).allowed  # keys are independent
    clock[0] += 20
    assert limiter.consume("k", limit).allowed
    assert not limiter.consume("k", limit).allowed


def test_token_bucket_memory_is_bounded() -> None:
    limiter = InProcessTokenBucket(max_keys=10)
    for index in range(100):
        limiter.consume(f"ip:{index}", RateLimit(1))
    assert len(limiter._buckets) == 10


def test_enforce_raises_a_429_problem_with_retry_after() -> None:
    limiter = InProcessTokenBucket(clock=lambda: 0.0)
    enforce(limiter, "k", RateLimit(1), "slow down")
    with pytest.raises(Problem) as raised:
        enforce(limiter, "k", RateLimit(1), "slow down")
    assert raised.value.status_code == 429
    assert raised.value.code == "rate_limited"
    assert raised.value.headers == {"Retry-After": "60"}


def test_rate_limit_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        RateLimit(0)


def test_enforce_quota() -> None:
    quotas = WorkspaceQuotas(
        storage_bytes=100, connected_accounts=2, posts_per_day=None, api_requests_per_minute=5
    )
    enforce_quota(quotas, "connected_accounts", used=1)
    enforce_quota(quotas, "posts_per_day", used=10_000)
    with pytest.raises(Problem) as raised:
        enforce_quota(quotas, "connected_accounts", used=2)
    assert (raised.value.status_code, raised.value.code) == (403, "quota_exceeded")
    with pytest.raises(Problem):
        enforce_quota(quotas, "storage_bytes", used=90, adding=11)
    enforce_quota(UNLIMITED, "storage_bytes", used=10**15)


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.9", "ip:203.0.113.9"),
        ("2001:db8::1", "ip:2001:db8::/64"),
        ("2001:db8:0:0:ffff:ffff:ffff:ffff", "ip:2001:db8::/64"),
        ("2001:db8:0:1::1", "ip:2001:db8:0:1::/64"),
        ("::ffff:198.51.100.7", "ip:198.51.100.7"),
        ("testclient", "ip:testclient"),
        (None, "ip:unknown"),
    ],
)
def test_client_key_folds_ipv6_to_its_64(address: str | None, key: str) -> None:
    assert client_key(address) == key
