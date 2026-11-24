"""The webhook per-source rate limiter stays bounded (RT1 review fix).

A source host is trivially spoofable, so a burst of distinct sources must not
leave a dict entry per spoofed source behind: a sweep drops every key whose
window emptied, and the dict is hard-capped with least-recently-active
eviction. Time is injected so the tests never sleep.
"""

from __future__ import annotations

from collections import deque

from runway.realtime.hooks_routes import _SourceRateLimiter


def test_distinct_sources_leave_no_residue_after_a_minute() -> None:
    """1,000 sources that go quiet are dropped entirely once the window passes."""
    moment = [0.0]
    limiter = _SourceRateLimiter(5, clock=lambda: moment[0])

    for index in range(1_000):
        assert limiter.allow(("youtube", f"source-{index}"))
    assert len(limiter._hits) == 1_000

    moment[0] = 61.0
    # The next request sweeps: every dead key is removed, only the live one stays.
    assert limiter.allow(("youtube", "still-here"))
    assert set(limiter._hits) == {("youtube", "still-here")}


def test_an_emptied_deque_is_deleted_and_treated_as_fresh() -> None:
    """A source re-activating after its window starts a brand-new deque."""
    moment = [0.0]
    limiter = _SourceRateLimiter(2, clock=lambda: moment[0])
    assert limiter.allow(("tiktok", "peer"))
    assert limiter.allow(("tiktok", "peer"))
    assert not limiter.allow(("tiktok", "peer"))

    moment[0] = 61.0
    assert limiter.allow(("tiktok", "peer"))
    assert limiter._hits[("tiktok", "peer")] == deque([61.0])


def test_the_dict_is_capped_and_evicts_the_least_recent_source() -> None:
    moment = [0.0]
    limiter = _SourceRateLimiter(5, clock=lambda: moment[0], max_keys=3)

    for index in range(5):
        assert limiter.allow(("instagram", f"src-{index}"))
        moment[0] += 0.001

    assert set(limiter._hits) == {
        ("instagram", "src-2"),
        ("instagram", "src-3"),
        ("instagram", "src-4"),
    }

    # Touching a live key refreshes its recency, so the next insert evicts src-3.
    assert limiter.allow(("instagram", "src-2"))
    assert limiter.allow(("instagram", "src-5"))
    assert set(limiter._hits) == {
        ("instagram", "src-2"),
        ("instagram", "src-4"),
        ("instagram", "src-5"),
    }


def test_a_denied_request_does_not_extend_the_window() -> None:
    moment = [0.0]
    limiter = _SourceRateLimiter(2, clock=lambda: moment[0])
    assert limiter.allow(("youtube", "a"))
    assert limiter.allow(("youtube", "a"))
    moment[0] = 30.0
    assert not limiter.allow(("youtube", "a"))
    assert limiter._hits[("youtube", "a")].count(30.0) == 0
    moment[0] = 61.0
    assert limiter.allow(("youtube", "a"))
