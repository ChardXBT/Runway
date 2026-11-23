"""A small, bounded reader for ISO base media (MP4/MOV) box layout.

ffprobe does not report where the ``moov`` box sits or whether tracks carry edit lists, and
Instagram rejects files with ``moov`` after ``mdat`` or with edit lists. This reader walks box
headers only (8 or 16 bytes each), seeking past box bodies, and visits at most
``MAX_BOXES`` boxes per walk, so a hostile file cannot make it read much or loop. When a walk
stops early (budget spent or a malformed box), an answer it could not reach is ``None``
(unknown), never ``False``.

Edit lists follow ``runway.platforms.limits.INSTAGRAM_EDIT_LIST_RULE``: any ``elst`` box with
at least one entry counts.
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
    # None when moov or mdat was not found at the top level (absent or not reached).
    moov_at_front: bool | None
    # True when any track has an elst box with entries. None when there is no moov box or the
    # walk inside moov stopped before it finished.
    has_edit_list: bool | None


class _Walk:
    """Box budget for one walk, and whether the walk stopped before the end."""

    def __init__(self, limit: int = MAX_BOXES) -> None:
        self.remaining = limit
        self.incomplete = False

    def spend(self) -> bool:
        if self.remaining <= 0:
            self.incomplete = True
            return False
        self.remaining -= 1
        return True


def _box_type(raw: bytes) -> str | None:
    if not all(0x20 <= byte <= 0x7E for byte in raw):
        return None
    return raw.decode("ascii")


def _iter_boxes(stream: BinaryIO, start: int, end: int, walk: _Walk) -> Iterator[Box]:
    position = start
    while position + _HEADER.size <= end:
        if not walk.spend():
            return
        stream.seek(position)
        header = stream.read(_HEADER.size)
        size, raw_type = _HEADER.unpack(header) if len(header) == _HEADER.size else (0, b"")
        box_type = _box_type(raw_type) if raw_type else None
        header_size = _HEADER.size
        if box_type is not None and size == 1:
            large = stream.read(_LARGE_SIZE.size)
            size = _LARGE_SIZE.unpack(large)[0] if len(large) == _LARGE_SIZE.size else 0
            header_size += _LARGE_SIZE.size
        elif box_type is not None and size == 0:
            size = end - position
        if box_type is None or size < header_size or position + size > end:
            walk.incomplete = True
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


def _elst_has_entries(stream: BinaryIO, elst: Box) -> bool | None:
    if elst.size < elst.header_size + _FULL_BOX_COUNT.size:
        return None
    stream.seek(elst.body_offset)
    _, entry_count = _FULL_BOX_COUNT.unpack(stream.read(_FULL_BOX_COUNT.size))
    return bool(entry_count > 0)


def _has_edit_list(stream: BinaryIO, moov: Box) -> bool | None:
    walk = _Walk()
    unknown = False
    for trak in _iter_boxes(stream, moov.body_offset, moov.end, walk):
        if trak.type != "trak":
            continue
        for edts in _iter_boxes(stream, trak.body_offset, trak.end, walk):
            if edts.type != "edts":
                continue
            for elst in _iter_boxes(stream, edts.body_offset, edts.end, walk):
                if elst.type != "elst":
                    continue
                found = _elst_has_entries(stream, elst)
                if found:
                    return True
                unknown = unknown or found is None
    return None if walk.incomplete or unknown else False


def read_layout(stream: BinaryIO) -> Mp4Layout | None:
    """Return the layout, or ``None`` when the stream does not start like an MP4/MOV file."""
    size = _stream_size(stream)
    boxes = list(_iter_boxes(stream, 0, size, _Walk()))
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
        has_edit_list=None if moov is None else _has_edit_list(stream, moov),
    )


def read_layout_from_path(path: Path) -> Mp4Layout | None:
    with open(path, "rb") as stream:
        return read_layout(stream)
