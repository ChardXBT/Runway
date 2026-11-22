from __future__ import annotations

import io

import pytest

from runway.mediastore import (
    ByteRange,
    instagram_poll_schedule,
    iter_tiktok_chunks,
    iter_youtube_chunks,
    plan_tiktok_chunks,
    read_range,
    youtube_chunk_ranges,
    youtube_resume_offset,
    youtube_status_query_headers,
)
from runway.platforms.limits import GB, MB, KiB, MiB

# YouTube ----------------------------------------------------------------------------------


def test_youtube_chunks_are_8_mib_with_a_short_last_chunk() -> None:
    size = 20 * MiB + 5
    ranges = youtube_chunk_ranges(size)
    assert [(r.first, r.last) for r in ranges] == [
        (0, 8 * MiB - 1),
        (8 * MiB, 16 * MiB - 1),
        (16 * MiB, size - 1),
    ]
    assert ranges[0].content_range == f"bytes 0-{8 * MiB - 1}/{size}"
    assert [r.is_last for r in ranges] == [False, False, True]
    assert all(r.length % (256 * KiB) == 0 for r in ranges[:-1])


def test_youtube_resumes_from_the_confirmed_offset() -> None:
    size = 20 * MiB
    # YouTube confirmed bytes 0-1000 (an offset that is not chunk-aligned).
    offset = youtube_resume_offset("bytes=0-1000")
    assert offset == 1001
    ranges = youtube_chunk_ranges(size, start_offset=offset)
    assert ranges[0].first == 1001
    assert ranges[0].length == 8 * MiB
    assert ranges[-1].last == size - 1
    assert sum(r.length for r in ranges) == size - 1001


def test_iter_youtube_chunks_reads_the_right_bytes() -> None:
    data = bytes(range(256)) * 5000
    chunk = 256 * KiB
    stream = io.BytesIO(data)
    chunks = list(iter_youtube_chunks(stream, len(data), start_offset=10, chunk_size=chunk))
    assert b"".join(c.data for c in chunks) == data[10:]
    assert chunks[0].range == ByteRange(10, 10 + chunk - 1, len(data))


@pytest.mark.parametrize(
    ("header", "offset"), [(None, 0), ("", 0), ("bytes=0-0", 1), ("bytes=0-8388607", 8388608)]
)
def test_youtube_resume_offset(header: str | None, offset: int) -> None:
    assert youtube_resume_offset(header) == offset


@pytest.mark.parametrize("header", ["bytes=5-10", "items=0-1", "bytes=0-", "0-10"])
def test_youtube_resume_offset_rejects_unexpected_headers(header: str) -> None:
    with pytest.raises(ValueError):
        youtube_resume_offset(header)


@pytest.mark.parametrize(
    ("size", "options"),
    [
        (0, {}),
        (10, {"chunk_size": 256 * KiB + 1}),
        (10, {"chunk_size": 0}),
        (10, {"start_offset": 10}),
        (10, {"start_offset": -1}),
        (256 * GB + 1, {}),
    ],
)
def test_youtube_chunk_ranges_reject_bad_input(size: int, options: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        youtube_chunk_ranges(size, **options)


def test_youtube_status_query_headers() -> None:
    assert youtube_status_query_headers(42) == {
        "Content-Length": "0",
        "Content-Range": "bytes */42",
    }


def test_short_stream_is_reported() -> None:
    with pytest.raises(EOFError):
        read_range(io.BytesIO(b"abc"), ByteRange(0, 9, 10))


# TikTok -----------------------------------------------------------------------------------


def test_tiktok_file_under_5_mb_is_one_chunk_of_its_own_size() -> None:
    plan = plan_tiktok_chunks(3 * MB)
    assert (plan.chunk_size, plan.total_chunk_count) == (3 * MB, 1)
    assert plan.chunks == (ByteRange(0, 3 * MB - 1, 3 * MB),)
    assert plan.source_info() == {
        "source": "FILE_UPLOAD",
        "video_size": 3 * MB,
        "chunk_size": 3 * MB,
        "total_chunk_count": 1,
    }


def test_tiktok_file_smaller_than_the_chunk_size_is_one_chunk() -> None:
    plan = plan_tiktok_chunks(7 * MB)
    assert (plan.chunk_size, plan.total_chunk_count) == (7 * MB, 1)


def test_tiktok_count_rounds_down_and_the_remainder_joins_the_last_chunk() -> None:
    # TikTok's own example: video_size 50000123, chunk_size 10000000, 5 chunks.
    plan = plan_tiktok_chunks(50_000_123)
    assert (plan.chunk_size, plan.total_chunk_count) == (10 * MB, 5)
    assert [c.length for c in plan.chunks] == [10 * MB] * 4 + [10 * MB + 123]
    assert plan.chunks[-1].content_range == "bytes 40000000-50000122/50000123"


def test_tiktok_last_chunk_can_be_almost_twice_the_chunk_size() -> None:
    size = 2 * 10 * MB - 1
    plan = plan_tiktok_chunks(size)
    assert plan.total_chunk_count == 1
    assert plan.chunks == (ByteRange(0, size - 1, size),)


def test_tiktok_largest_file_and_chunk_bounds() -> None:
    plan = plan_tiktok_chunks(4 * GB, 64 * MB)
    assert plan.total_chunk_count == 62
    assert plan.chunks[-1].length <= 128 * MB
    assert sum(c.length for c in plan.chunks) == 4 * GB
    plan = plan_tiktok_chunks(4 * GB, 5 * MiB)
    assert plan.total_chunk_count <= 1000


@pytest.mark.parametrize(
    ("size", "chunk"),
    [(0, 10 * MB), (4 * GB + 1, 10 * MB), (100 * MB, 5 * MB), (100 * MB, 64 * MB + 1)],
)
def test_tiktok_rejects_sizes_outside_the_rules(size: int, chunk: int) -> None:
    with pytest.raises(ValueError):
        plan_tiktok_chunks(size, chunk)


def test_iter_tiktok_chunks_covers_the_file_in_order() -> None:
    data = bytes(range(256)) * 50_000  # 12.8 MB
    plan = plan_tiktok_chunks(len(data), 6 * MiB)
    chunks = list(iter_tiktok_chunks(io.BytesIO(data), plan))
    assert [c.range for c in chunks] == list(plan.chunks)
    assert b"".join(c.data for c in chunks) == data


# Instagram --------------------------------------------------------------------------------


def test_instagram_poll_schedule() -> None:
    schedule = instagram_poll_schedule()
    assert schedule[:5] == (60, 120, 180, 240, 300)
    assert schedule[5:7] == (600, 900)
    assert schedule[-1] == 7200
    assert all(later > earlier for earlier, later in zip(schedule, schedule[1:], strict=False))
    assert instagram_poll_schedule(180) == (60, 120, 180)
    with pytest.raises(ValueError):
        instagram_poll_schedule(0)
