"""Chunking and timing for sending media to platforms (spec 02 §6). Pure functions only.

The publishers (spec 03 and 08) do the HTTP; these helpers decide which bytes go in which
request, so the rules can be tested without a network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from runway.platforms.limits import (
    INSTAGRAM_POLLING,
    TIKTOK_CHUNKS,
    TIKTOK_VIDEO,
    YOUTUBE_UPLOAD,
)

_RANGE_HEADER_RE = re.compile(r"bytes=0-(\d+)")


@dataclass(frozen=True)
class ByteRange:
    """An inclusive byte range of a file of ``total`` bytes."""

    first: int
    last: int
    total: int

    @property
    def length(self) -> int:
        return self.last - self.first + 1

    @property
    def is_last(self) -> bool:
        return self.last == self.total - 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.first}-{self.last}/{self.total}"


@dataclass(frozen=True)
class Chunk:
    range: ByteRange
    data: bytes


def read_range(stream: BinaryIO, byte_range: ByteRange) -> bytes:
    """Read exactly ``byte_range`` from a seekable stream."""
    stream.seek(byte_range.first)
    parts: list[bytes] = []
    remaining = byte_range.length
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            raise EOFError(
                f"media ended before byte {byte_range.last} of {byte_range.total}; "
                "the stored file is shorter than its recorded size"
            )
        parts.append(block)
        remaining -= len(block)
    return b"".join(parts)


# YouTube ----------------------------------------------------------------------------------


def youtube_chunk_ranges(
    total_size: int,
    *,
    start_offset: int = 0,
    chunk_size: int = YOUTUBE_UPLOAD.default_chunk_bytes,
) -> list[ByteRange]:
    """Byte ranges for a resumable upload, from the first byte YouTube has not confirmed.

    Every chunk except the last is ``chunk_size`` bytes, which must be a multiple of 256 KiB.
    """
    if total_size <= 0:
        raise ValueError("total_size must be positive")
    if total_size > YOUTUBE_UPLOAD.max_size_bytes:
        raise ValueError("file is larger than YouTube accepts")
    multiple = YOUTUBE_UPLOAD.chunk_multiple_bytes
    if chunk_size <= 0 or chunk_size % multiple:
        raise ValueError(f"chunk_size must be a positive multiple of {multiple} bytes")
    if not 0 <= start_offset < total_size:
        raise ValueError("start_offset must be within the file")
    ranges: list[ByteRange] = []
    first = start_offset
    while first < total_size:
        last = min(first + chunk_size, total_size) - 1
        ranges.append(ByteRange(first, last, total_size))
        first = last + 1
    return ranges


def iter_youtube_chunks(
    stream: BinaryIO,
    total_size: int,
    *,
    start_offset: int = 0,
    chunk_size: int = YOUTUBE_UPLOAD.default_chunk_bytes,
) -> Iterator[Chunk]:
    """Yield chunks to ``PUT`` with ``Content-Range``, reading one chunk at a time."""
    for byte_range in youtube_chunk_ranges(
        total_size, start_offset=start_offset, chunk_size=chunk_size
    ):
        yield Chunk(byte_range, read_range(stream, byte_range))


def youtube_resume_offset(range_header: str | None) -> int:
    """First byte to send after a ``308``: ``Range: bytes=0-N`` means resume at ``N + 1``.

    No ``Range`` header means YouTube has nothing yet.
    """
    if range_header is None or not range_header.strip():
        return 0
    match = _RANGE_HEADER_RE.fullmatch(range_header.strip())
    if match is None:
        raise ValueError("unexpected Range header from YouTube")
    return int(match.group(1)) + 1


def youtube_status_query_headers(total_size: int) -> dict[str, str]:
    """Headers for the empty ``PUT`` that asks a session how much has arrived."""
    return {"Content-Length": "0", "Content-Range": f"bytes */{total_size}"}


# TikTok -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TikTokChunkPlan:
    """Values for ``source_info`` in the init call, and the ranges to ``PUT`` in order."""

    video_size: int
    chunk_size: int
    total_chunk_count: int
    chunks: tuple[ByteRange, ...]

    def source_info(self) -> dict[str, int | str]:
        return {
            "source": "FILE_UPLOAD",
            "video_size": self.video_size,
            "chunk_size": self.chunk_size,
            "total_chunk_count": self.total_chunk_count,
        }


def plan_tiktok_chunks(
    video_size: int, chunk_size: int = TIKTOK_CHUNKS.default_chunk_bytes
) -> TikTokChunkPlan:
    """Plan a ``FILE_UPLOAD`` under the Content Posting API rules (limits ``TIKTOK_CHUNKS``).

    - A file no larger than ``chunk_size`` (so any file under 5 MB) is one chunk of its own
      size, unless it is above the 64 MB single-chunk limit.
    - Otherwise ``total_chunk_count = floor(video_size / chunk_size)``, every chunk but the
      last is ``chunk_size`` and the remainder is folded into the last chunk (at most 128 MB).
    - A file that would be a single chunk above 64 MB is split instead: ``chunk_size``
      becomes ``video_size // 2``, giving two chunks of at most 64 MB each.
    """
    rules = TIKTOK_CHUNKS
    if video_size <= 0:
        raise ValueError("video_size must be positive")
    if video_size > TIKTOK_VIDEO.max_size_bytes:
        raise ValueError("file is larger than TikTok accepts")
    if not rules.min_chunk_bytes <= chunk_size <= rules.max_chunk_bytes:
        raise ValueError(
            f"chunk_size must be between {rules.min_chunk_bytes} and {rules.max_chunk_bytes}"
        )
    if video_size // chunk_size <= 1:
        if video_size <= rules.max_single_chunk_bytes:
            whole = ByteRange(0, video_size - 1, video_size)
            return TikTokChunkPlan(video_size, video_size, 1, (whole,))
        # Between 64 MB and 2 x chunk_size: two chunks of half the file each.
        chunk_size = video_size // 2

    count = video_size // chunk_size
    if count > rules.max_chunk_count:
        raise ValueError(
            f"chunk_size is too small: TikTok accepts at most {rules.max_chunk_count} chunks"
        )
    chunks = [
        ByteRange(index * chunk_size, (index + 1) * chunk_size - 1, video_size)
        for index in range(count - 1)
    ]
    chunks.append(ByteRange((count - 1) * chunk_size, video_size - 1, video_size))
    if any(chunk.length > rules.max_chunk_bytes for chunk in chunks[:-1]):
        raise ValueError("a chunk other than the last would exceed TikTok's chunk limit")
    if chunks[-1].length > rules.max_final_chunk_bytes:
        raise ValueError("the final chunk would exceed TikTok's limit")
    return TikTokChunkPlan(video_size, chunk_size, count, tuple(chunks))


def iter_tiktok_chunks(stream: BinaryIO, plan: TikTokChunkPlan) -> Iterator[Chunk]:
    for byte_range in plan.chunks:
        yield Chunk(byte_range, read_range(stream, byte_range))


# Instagram --------------------------------------------------------------------------------


def instagram_poll_schedule(
    url_ttl_seconds: int = int(INSTAGRAM_POLLING.signed_url_ttl.total_seconds()),
) -> tuple[int, ...]:
    """Seconds after container creation at which to read ``status_code``.

    Once a minute for the documented five minutes, then every five minutes until the signed
    media URL expires (spec 02 §6).
    """
    if url_ttl_seconds <= 0:
        raise ValueError("url_ttl_seconds must be positive")
    step = int(INSTAGRAM_POLLING.poll_interval.total_seconds())
    window = int(INSTAGRAM_POLLING.documented_window.total_seconds())
    extended = int(INSTAGRAM_POLLING.extended_interval.total_seconds())
    times = list(range(step, min(window, url_ttl_seconds) + 1, step))
    elapsed = window + extended
    while elapsed <= url_ttl_seconds:
        times.append(elapsed)
        elapsed += extended
    return tuple(times)
