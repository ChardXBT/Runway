"""A small, bounded reader for ISO base media (MP4/MOV) box layout.

ffprobe does not report where the ``moov`` box sits or whether tracks carry edit lists, and
Instagram rejects files with ``moov`` after ``mdat`` or with edit lists. This reader walks box
headers only (8 or 16 bytes each), seeking past box bodies, and visits at most
``MAX_BOXES`` boxes, so a hostile file cannot make it read much or loop.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_BOXES = 20_000
_HEADER = struct.Struct(">I4s")
_LARGE_SIZE = struct.Struct(">Q")
_FULL_BOX_COUNT = struct.Struct(">BxxxI")
_ELST_ENTRY_V0 = struct.Struct(">IihH")
_ELST_ENTRY_V1 = struct.Struct(">QqhH")
# Box types an MP4 or QuickTime file can start with.
_FIRST_BOX_TYPES = frozenset({"ftyp", "moov", "mdat", "wide", "free", "skip", "pnot"})


@dataclass(frozen=True)
class Box:
    type: str
    offset: int
    size: int
    header_size: int

    @property
    def body_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class Mp4Layout:
    top_level: tuple[str, ...]
    major_brand: str | None
    # None when the file has no moov or no mdat at the top level.
    moov_at_front: bool | None
    # True when a track has an edit list that cuts, pauses or re-times it (see
    # _edit_list_changes_timeline). None when the file has no moov box.
    has_edit_list: bool | None


class _Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def spend(self) -> bool:
        self.remaining -= 1
        return self.remaining >= 0


def _box_type(raw: bytes) -> str | None:
    if not all(0x20 <= byte <= 0x7E for byte in raw):
        return None
    return raw.decode("ascii")


def _iter_boxes(stream: BinaryIO, start: int, end: int, budget: _Budget) -> Iterator[Box]:
    position = start
    while position + _HEADER.size <= end and budget.spend():
        stream.seek(position)
        header = stream.read(_HEADER.size)
        if len(header) < _HEADER.size:
            return
        size, raw_type = _HEADER.unpack(header)
        box_type = _box_type(raw_type)
        if box_type is None:
            return
        header_size = _HEADER.size
        if size == 1:
            large = stream.read(_LARGE_SIZE.size)
            if len(large) < _LARGE_SIZE.size:
                return
            (size,) = _LARGE_SIZE.unpack(large)
            header_size += _LARGE_SIZE.size
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            return
        yield Box(box_type, position, size, header_size)
        position += size


def _stream_size(stream: BinaryIO) -> int:
    current = stream.tell()
    size = stream.seek(0, 2)
    stream.seek(current)
    return size


def _major_brand(stream: BinaryIO, box: Box) -> str | None:
    if box.size < box.header_size + 4:
        return None
    stream.seek(box.body_offset)
    return _box_type(stream.read(4))


def _edit_list_changes_timeline(stream: BinaryIO, elst: Box) -> bool:
    """True when the edit list cuts, pauses or re-times the track.

    A single full-rate entry only shifts the track start. ffmpeg and phone cameras write one
    on nearly every file, so it is not counted.
    """
    if elst.size < elst.header_size + _FULL_BOX_COUNT.size:
        return False
    stream.seek(elst.body_offset)
    version, entry_count = _FULL_BOX_COUNT.unpack(stream.read(_FULL_BOX_COUNT.size))
    if entry_count == 0:
        return False
    if entry_count > 1:
        return True
    entry = _ELST_ENTRY_V1 if version == 1 else _ELST_ENTRY_V0
    raw = stream.read(entry.size)
    if len(raw) < entry.size:
        return True
    _, media_time, rate_integer, rate_fraction = entry.unpack(raw)
    return bool(media_time == -1 or rate_integer != 1 or rate_fraction != 0)


def _has_edit_list(stream: BinaryIO, moov: Box, budget: _Budget) -> bool:
    for trak in _iter_boxes(stream, moov.body_offset, moov.end, budget):
        if trak.type != "trak":
            continue
        for edts in _iter_boxes(stream, trak.body_offset, trak.end, budget):
            if edts.type != "edts":
                continue
            for elst in _iter_boxes(stream, edts.body_offset, edts.end, budget):
                if elst.type == "elst" and _edit_list_changes_timeline(stream, elst):
                    return True
    return False


def read_layout(stream: BinaryIO) -> Mp4Layout | None:
    """Return the layout, or ``None`` when the stream does not start like an MP4/MOV file."""
    budget = _Budget(MAX_BOXES)
    size = _stream_size(stream)
    boxes = list(_iter_boxes(stream, 0, size, budget))
    if not boxes:
        return None
    types = tuple(box.type for box in boxes)
    if types[0] not in _FIRST_BOX_TYPES:
        return None
    ftyp = next((box for box in boxes if box.type == "ftyp"), None)
    moov = next((box for box in boxes if box.type == "moov"), None)
    mdat_index = types.index("mdat") if "mdat" in types else None
    moov_index = types.index("moov") if "moov" in types else None
    moov_at_front = None if moov_index is None or mdat_index is None else moov_index < mdat_index
    return Mp4Layout(
        top_level=types,
        major_brand=_major_brand(stream, ftyp) if ftyp else None,
        moov_at_front=moov_at_front,
        has_edit_list=None if moov is None else _has_edit_list(stream, moov, budget),
    )


def read_layout_from_path(path: Path) -> Mp4Layout | None:
    with open(path, "rb") as stream:
        return read_layout(stream)
