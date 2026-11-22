from __future__ import annotations

import io
import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runway.mediastore import (
    ALLOWED_DEMUXERS,
    PROBE_INPUT_SUFFIX,
    MediaProber,
    MediaToolMissing,
    MediaTools,
    ProbeError,
    ProbeResult,
    ProbeTimeout,
    default_poster_time_ms,
    new_probe_input,
    parse_ffprobe,
    safe_tool_message,
)
from runway.mediastore import probe as probe_module
from runway.mediastore.mp4atoms import MAX_BOXES, Mp4Layout, read_layout

FIXTURES = Path(__file__).parent / "fixtures" / "ffprobe"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ffprobe JSON parsing --------------------------------------------------------------------


def test_parses_h264_aac_mp4() -> None:
    result = parse_ffprobe(_fixture("h264_aac_vertical_mp4.json"))
    assert result.kind == "video"
    assert result.container == "mp4"
    assert (result.video_codec, result.width, result.height, result.fps) == (
        "h264",
        1080,
        1920,
        30.0,
    )
    assert (result.duration_ms, result.bitrate, result.size_bytes) == (4000, 15828504, 7914252)
    assert (result.audio_codec, result.audio_sample_rate, result.audio_channels) == (
        "aac",
        48000,
        2,
    )
    assert result.aspect_ratio == pytest.approx(9 / 16)
    assert result.moov_at_front is None and result.has_edit_list is None


def test_layout_and_actual_size_override_the_ffprobe_values() -> None:
    layout = Mp4Layout(("ftyp", "mdat", "moov"), "isom", moov_at_front=False, has_edit_list=True)
    result = parse_ffprobe(_fixture("h264_aac_vertical_mp4.json"), layout=layout, size_bytes=5)
    assert (result.moov_at_front, result.has_edit_list, result.size_bytes) == (False, True, 5)


def test_rotation_metadata_swaps_display_dimensions_and_mov_is_named() -> None:
    result = parse_ffprobe(_fixture("hevc_rotated_mov.json"))
    assert (result.container, result.video_codec, result.rotation) == ("mov", "hevc", 90)
    assert (result.width, result.height) == (1080, 1920)
    assert result.audio_codec is None


def test_legacy_rotate_tag_is_honoured() -> None:
    payload = _fixture("hevc_rotated_mov.json")
    stream = payload["streams"][0]
    del stream["side_data_list"]
    stream["tags"]["rotate"] = "270"
    result = parse_ffprobe(payload)
    assert (result.rotation, result.width, result.height) == (270, 1080, 1920)


def test_webm_is_told_apart_from_matroska_by_codec() -> None:
    webm = parse_ffprobe(_fixture("vp9_opus_webm.json"))
    assert (webm.container, webm.video_codec, webm.audio_codec, webm.fps) == (
        "webm",
        "vp9",
        "opus",
        25.0,
    )
    assert webm.duration_ms == 2008
    payload = _fixture("vp9_opus_webm.json")
    payload["streams"][0]["codec_name"] = "h264"
    assert parse_ffprobe(payload).container == "matroska"


def test_type_comes_from_content_not_the_filename() -> None:
    # A JPEG uploaded as "photo.mp4": ffprobe identifies it by its bytes.
    jpeg = parse_ffprobe(_fixture("jpeg_named_mp4.json"))
    assert (jpeg.kind, jpeg.container, jpeg.width, jpeg.height) == ("image", "jpeg", 1080, 1350)
    assert jpeg.fps is None and jpeg.duration_ms is None and jpeg.bitrate is None
    png = parse_ffprobe(_fixture("png_image.json"))
    assert (png.kind, png.container) == ("image", "png")


def test_audio_only_and_cover_art_are_not_video() -> None:
    audio = parse_ffprobe(_fixture("aac_audio_only_m4a.json"))
    assert audio.kind == "audio" and audio.width is None
    payload = _fixture("aac_audio_only_m4a.json")
    payload["streams"].append(
        {
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 500,
            "height": 500,
            "disposition": {"attached_pic": 1},
        }
    )
    assert parse_ffprobe(payload).kind == "audio"


def test_empty_output_is_unknown() -> None:
    result = parse_ffprobe({})
    assert (result.kind, result.container, result.duration_ms) == ("unknown", None, None)


def test_result_round_trips_through_json() -> None:
    result = parse_ffprobe(_fixture("h264_aac_vertical_mp4.json"))
    restored = ProbeResult.from_dict(json.loads(json.dumps(result.to_dict())))
    assert restored == result


def test_default_poster_time() -> None:
    video = parse_ffprobe(_fixture("h264_aac_vertical_mp4.json"))
    assert default_poster_time_ms(video) == 1000
    short = parse_ffprobe(_fixture("vp9_opus_webm.json"))
    assert default_poster_time_ms(short) == 1000
    image = parse_ffprobe(_fixture("png_image.json"))
    assert default_poster_time_ms(image) == 0


# MP4 box layout ----------------------------------------------------------------------------


def _box(box_type: str, body: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(body), box_type.encode()) + body


def _elst(entries: list[tuple[int, int, int, int]], version: int = 0) -> bytes:
    shape = ">QqhH" if version == 1 else ">IihH"
    body = struct.pack(">BxxxI", version, len(entries))
    body += b"".join(struct.pack(shape, *entry) for entry in entries)
    return _box("elst", body)


def _moov(*elsts: bytes) -> bytes:
    traks = b"".join(_box("trak", _box("tkhd", b"\0" * 8) + _box("edts", e)) for e in elsts)
    return _box("moov", _box("mvhd", b"\0" * 8) + traks)


FTYP = _box("ftyp", b"isom\0\0\x02\0isomiso2")
IDENTITY = (1000, 0, 1, 0)


def _layout(data: bytes) -> Mp4Layout | None:
    return read_layout(io.BytesIO(data))


def test_moov_position() -> None:
    front = _layout(FTYP + _moov() + _box("mdat", b"x" * 32))
    assert front is not None and front.moov_at_front is True and front.major_brand == "isom"
    back = _layout(FTYP + _box("mdat", b"x" * 32) + _moov())
    assert back is not None and back.moov_at_front is False
    no_mdat = _layout(FTYP + _moov())
    assert no_mdat is not None and no_mdat.moov_at_front is None


def test_large_and_open_ended_box_sizes() -> None:
    large_mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + 4) + b"data"
    layout = _layout(FTYP + large_mdat + _moov())
    assert layout is not None and layout.top_level == ("ftyp", "mdat", "moov")
    to_end = struct.pack(">I4s", 0, b"mdat") + b"rest of file"
    layout = _layout(FTYP + _moov() + to_end)
    assert layout is not None and layout.top_level == ("ftyp", "moov", "mdat")


@pytest.mark.parametrize(
    ("elsts", "expected"),
    [
        ([], False),
        ([_elst([])], False),
        # Any elst entry counts, including ffmpeg's usual single start shift
        # (limits.INSTAGRAM_EDIT_LIST_RULE).
        ([_elst([IDENTITY])], True),
        ([_elst([(1000, 1024, 1, 0)])], True),
        ([_elst([IDENTITY], version=1)], True),
        ([_elst([]), _elst([(500, -1, 1, 0), IDENTITY])], True),
        ([_elst([IDENTITY, IDENTITY])], True),
    ],
)
def test_any_edit_list_entry_counts(elsts: list[bytes], expected: bool) -> None:
    layout = _layout(FTYP + _moov(*elsts) + _box("mdat"))
    assert layout is not None and layout.has_edit_list is expected


def test_exhausted_top_level_budget_still_reads_the_moov() -> None:
    # Before the fix the 20,000 free boxes spent the shared budget and the moov walk
    # reported "no edit list" (False) for a file that has one.
    data = FTYP + _moov(_elst([(500, -1, 1, 0), IDENTITY])) + _box("mdat") + _box("free") * 20_000
    layout = _layout(data)
    assert layout is not None
    assert layout.moov_at_front is True
    assert layout.has_edit_list is True


def test_exhausted_budget_inside_moov_is_unknown_not_false() -> None:
    padding = _box("free") * (MAX_BOXES + 10)
    trak = _box("trak", _box("edts", _elst([IDENTITY])))
    data = FTYP + _box("moov", padding + trak) + _box("mdat")
    layout = _layout(data)
    assert layout is not None and layout.moov_at_front is True
    assert layout.has_edit_list is None


def test_moov_beyond_an_exhausted_top_level_walk_is_unknown() -> None:
    data = FTYP + _box("free") * (MAX_BOXES + 10) + _box("mdat") + _moov(_elst([IDENTITY]))
    layout = _layout(data)
    assert layout is not None
    assert layout.moov_at_front is None and layout.has_edit_list is None


def test_malformed_box_inside_moov_is_unknown() -> None:
    broken_trak = struct.pack(">I4s", 9_999, b"trak") + b"\0" * 8
    data = FTYP + _box("moov", broken_trak) + _box("mdat")
    layout = _layout(data)
    assert layout is not None and layout.has_edit_list is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x1aE\xdf\xa3" + b"\0" * 40,  # Matroska/WebM EBML header
        b"\xff\xd8\xff\xe0" + b"\0" * 40,  # JPEG
        _box("abcd") + _box("mdat"),
    ],
)
def test_non_mp4_input_has_no_layout(data: bytes) -> None:
    assert _layout(data) is None


def test_corrupt_sizes_stop_the_walk() -> None:
    truncated = FTYP + struct.pack(">I4s", 10_000, b"moov") + b"short"
    layout = _layout(truncated)
    assert layout is not None and layout.top_level == ("ftyp",)
    too_small = FTYP + struct.pack(">I4s", 4, b"mdat") + b"\0" * 16
    layout = _layout(too_small)
    assert layout is not None and layout.top_level == ("ftyp",)


def test_box_count_is_bounded() -> None:
    data = FTYP + _box("free") * (MAX_BOXES + 5_000)
    layout = _layout(data)
    assert layout is not None and len(layout.top_level) <= MAX_BOXES


# Running ffprobe ---------------------------------------------------------------------------


def _input(tmp_path: Path, content: bytes = b"x") -> Path:
    media = new_probe_input(tmp_path)
    media.write_bytes(content)
    return media


def test_allowed_demuxers_are_exactly_what_validation_accepts() -> None:
    assert set(ALLOWED_DEMUXERS) == {
        "mov",
        "mp4",
        "m4a",
        "3gp",
        "3g2",
        "mj2",
        "matroska",
        "webm",
        "jpeg_pipe",
    }
    # Demuxers that open further files by name must never be allowed.
    assert not {"hls", "concat", "dash", "image2", "tee", "mpd"} & set(ALLOWED_DEMUXERS)


def test_missing_binary_gives_a_clear_error_without_paths(tmp_path: Path) -> None:
    media = _input(tmp_path)
    configured = str(tmp_path / "tools" / "runway-no-such-ffprobe.exe")
    prober = MediaProber(MediaTools(ffprobe=configured, ffmpeg="runway-no-ffmpeg"))
    with pytest.raises(MediaToolMissing) as raised:
        prober.probe(media)
    assert "'runway-no-such-ffprobe.exe'" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    with pytest.raises(MediaToolMissing, match="Install FFmpeg"):
        prober.extract_poster(media, tmp_path / "poster.jpg")


def test_only_server_named_inputs_are_accepted(tmp_path: Path) -> None:
    prober = MediaProber()
    with pytest.raises(TypeError):
        prober.probe("http://example.com/x.mp4")  # type: ignore[arg-type]
    for client_named in ("clip.mp4", "list.m3u8", "x.ffconcat", "noext"):
        path = tmp_path / client_named
        path.write_bytes(b"x")
        with pytest.raises(ProbeError, match="server-named"):
            prober.probe(path)
        with pytest.raises(ProbeError, match="server-named"):
            prober.extract_poster(path, tmp_path / "poster.jpg")
    with pytest.raises(ProbeError, match="does not exist"):
        prober.probe(tmp_path / f"missing{PROBE_INPUT_SUFFIX}")
    with pytest.raises(ProbeError, match=".jpg"):
        prober.extract_poster(_input(tmp_path), tmp_path / "poster.png")


def _fake_tools() -> MediaTools:
    # Any existing executable resolves; subprocess.run is replaced in these tests.
    return MediaTools(ffprobe=sys.executable, ffmpeg=sys.executable, timeout_seconds=7)


def test_probe_and_poster_arguments_are_locked_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _input(tmp_path, b"not really a video")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        if "-print_format" in arguments:
            stdout = (FIXTURES / "h264_aac_vertical_mp4.json").read_bytes()
            return subprocess.CompletedProcess(arguments, 0, stdout, b"")
        Path(arguments[-1].removeprefix("file:")).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(probe_module.subprocess, "run", fake_run)
    prober = MediaProber(_fake_tools())
    result = prober.probe(media)
    prober.extract_poster(media, tmp_path / "poster.jpg")
    for arguments, kwargs in calls:
        assert arguments[arguments.index("-protocol_whitelist") + 1] == "file"
        assert arguments[arguments.index("-format_whitelist") + 1] == ",".join(ALLOWED_DEMUXERS)
        assert f"file:{media.resolve()}" in arguments
        input_at = arguments.index(f"file:{media.resolve()}")
        assert arguments.index("-format_whitelist") < input_at
        assert kwargs["timeout"] == 7 and kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs
    poster_arguments = calls[1][0]
    assert poster_arguments[poster_arguments.index("-update") + 1] == "1"
    # The file is not really an MP4, so the layout is unknown, and the size is the real one.
    assert result.moov_at_front is None and result.size_bytes == media.stat().st_size


def test_probe_timeout_and_failure_messages_carry_no_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _input(tmp_path)

    def slow(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

    monkeypatch.setattr(probe_module.subprocess, "run", slow)
    with pytest.raises(ProbeTimeout, match="7 seconds"):
        MediaProber(_fake_tools()).probe(media)

    stderr = (
        f"[hls @ 0000019df7b35a40] Format not on whitelist 'mov,mp4'\n"
        f"file:{media.resolve()}: Invalid data found when processing input\n"
    ).encode()

    def failing(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 1, b"", stderr)

    monkeypatch.setattr(probe_module.subprocess, "run", failing)
    with pytest.raises(ProbeError) as raised:
        MediaProber(_fake_tools()).probe(media)
    message = str(raised.value)
    assert "Invalid data found when processing input" in message
    assert str(tmp_path) not in message and "C:\\" not in message and "file:" not in message
    with pytest.raises(ProbeError) as raised:
        MediaProber(_fake_tools()).extract_poster(media, tmp_path / "out" / "poster.jpg")
    assert str(tmp_path) not in str(raised.value)
    assert not (tmp_path / "out" / "poster.jpg").exists()

    def not_json(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 0, b"[1, 2]", b"")

    monkeypatch.setattr(probe_module.subprocess, "run", not_json)
    with pytest.raises(ProbeError, match="JSON object"):
        MediaProber(_fake_tools()).probe(media)


@pytest.mark.parametrize(
    "line",
    [
        "file:C:\\Users\\someone\\data\\media\\w\\1\\a\\x.input: Invalid argument",
        "Error opening input file file:C:\\data\\x y.input.",
        "/srv/runway/data/media/.scratch/abc.input: Operation not permitted",
        "\\\\server\\share\\x.input: No such file",
        "[concat @ 000001e8c5a65a00] Unsafe file name 'C:/Users/ericy/victim.mp4'",
    ],
)
def test_safe_tool_message_removes_paths(line: str) -> None:
    message = safe_tool_message(f"noise\n{line}\n")
    assert "<file>" in message or "Unsafe" in message
    for fragment in ("C:\\", "C:/", "/srv/", "\\\\server", "Users", "ericy"):
        assert fragment not in message
    assert " @ " not in message
