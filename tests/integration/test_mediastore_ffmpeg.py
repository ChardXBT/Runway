"""Probe and poster extraction against the real ffmpeg binaries, on tiny generated clips.

Skipped when ffmpeg or ffprobe is not on PATH. Clips are generated with ``-f lavfi`` into
``tmp_path``; nothing is downloaded and no video is committed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from runway.mediastore import (
    LocalDiskStore,
    MediaProber,
    ProbeError,
    make_media_key,
    new_probe_input,
    validate,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are not on PATH",
)


def _ffmpeg(*arguments: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", *arguments],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _clip(
    path: Path,
    *,
    faststart: bool,
    edit_list: bool = True,
    seconds: str = "3.5",
    size: str = "360x640",
) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", seconds, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2",
        *(["-movflags", "+faststart"] if faststart else []),
        *([] if edit_list else ["-use_editlist", "0"]),
        str(path),
    )  # fmt: skip
    return path


def _as_input(source: Path, directory: Path) -> Path:
    """What the probe job does: a server-named copy of the stored bytes."""
    target = new_probe_input(directory)
    shutil.copyfile(source, target)
    return target


@pytest.fixture(scope="module")
def prober() -> MediaProber:
    return MediaProber()


def test_clean_clip_is_probed_and_valid_for_every_video_target(
    tmp_path: Path, prober: MediaProber
) -> None:
    clip = _clip(tmp_path / "clip.mp4", faststart=True, edit_list=False)
    probe_input = _as_input(clip, tmp_path)
    result = prober.probe(probe_input)
    assert (result.kind, result.container, result.video_codec, result.audio_codec) == (
        "video",
        "mp4",
        "h264",
        "aac",
    )
    assert (result.width, result.height) == (360, 640)
    # Without an edit list the first timestamps are offset, so the average rate is ~29.8.
    assert result.fps == pytest.approx(30.0, abs=0.5)
    assert result.duration_ms is not None and 3_400 <= result.duration_ms <= 3_600
    assert (result.audio_sample_rate, result.audio_channels) == (48_000, 2)
    assert result.moov_at_front is True and result.has_edit_list is False
    assert result.size_bytes == clip.stat().st_size
    for platform, post_type in [
        ("youtube", "youtube_video"),
        ("youtube", "youtube_short"),
        ("instagram", "instagram_reel"),
        ("instagram", "instagram_story"),
        ("tiktok", "tiktok_video"),
    ]:
        errors = [i for i in validate(result, platform, post_type) if i.severity == "error"]
        assert errors == [], (post_type, errors)


def test_layout_problems_are_detected_and_the_named_fixes_work(
    tmp_path: Path, prober: MediaProber
) -> None:
    # ffmpeg's default MP4 output: moov at the end and an AAC-priming edit list.
    clip = _clip(tmp_path / "default.mp4", faststart=False)
    result = prober.probe(_as_input(clip, tmp_path))
    assert result.moov_at_front is False and result.has_edit_list is True
    issues = [i for i in validate(result, "instagram", "instagram_reel") if i.severity == "error"]
    assert {i.code for i in issues} == {"moov_not_at_front", "edit_list_present"}
    edit_fix = next(i.fix for i in issues if i.code == "edit_list_present")
    assert edit_fix is not None and "-use_editlist 0" in edit_fix

    fixed = tmp_path / "fixed.mp4"
    _ffmpeg("-i", str(clip), "-c", "copy", "-use_editlist", "0", "-movflags", "+faststart",
            str(fixed))  # fmt: skip
    repaired = prober.probe(_as_input(fixed, tmp_path))
    assert repaired.moov_at_front is True and repaired.has_edit_list is False


def test_type_is_decided_by_content_not_extension(tmp_path: Path, prober: MediaProber) -> None:
    jpeg = tmp_path / "photo.jpg"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1350", "-frames:v", "1", str(jpeg))
    result = prober.probe(_as_input(jpeg, tmp_path))
    assert (result.kind, result.container, result.width, result.height) == (
        "image",
        "jpeg",
        1080,
        1350,
    )
    assert validate(result, "instagram", "instagram_image") == []

    webm = tmp_path / "clip.webm"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "1",
        "-c:v", "libvpx-vp9", "-deadline", "realtime", str(webm),
    )  # fmt: skip
    probed = prober.probe(_as_input(webm, tmp_path))
    assert (probed.kind, probed.container, probed.moov_at_front) == ("video", "webm", None)


def _victim(tmp_path: Path) -> Path:
    """Another workspace's stored video, at its real path inside the media root."""
    victim = tmp_path / "media" / "w" / "other" / "asset" / "0123456789abcdef.mp4"
    victim.parent.mkdir(parents=True)
    return _clip(victim, faststart=True, seconds="1", size="320x240")


@pytest.mark.parametrize(
    "playlist",
    [
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:1.0,\nfile:{victim}\n#EXT-X-ENDLIST\n",
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:1.0,\n{victim}\n#EXT-X-ENDLIST\n",
        "ffconcat version 1.0\nfile '{victim_posix}'\n",
    ],
    ids=["hls-file-url", "hls-path", "concat"],
)
def test_playlists_pointing_at_other_files_are_refused(
    tmp_path: Path, prober: MediaProber, playlist: str
) -> None:
    victim = _victim(tmp_path)
    upload = playlist.format(victim=victim, victim_posix=victim.as_posix()).encode()
    # Both under the client's name (as the old code probed it) and under the server's name.
    for name in ("upload.m3u8", "upload.ffconcat"):
        (tmp_path / name).write_bytes(upload)
    probe_input = new_probe_input(tmp_path)
    probe_input.write_bytes(upload)

    with pytest.raises(ProbeError) as raised:
        prober.probe(probe_input)
    assert str(tmp_path) not in str(raised.value)
    poster = tmp_path / "poster.jpg"
    with pytest.raises(ProbeError):
        prober.extract_poster(probe_input, poster)
    assert not poster.exists()
    for name in ("upload.m3u8", "upload.ffconcat"):
        with pytest.raises(ProbeError):
            prober.probe(tmp_path / name)


def test_probe_job_flow_through_the_local_store(tmp_path: Path, prober: MediaProber) -> None:
    clip = _clip(tmp_path / "clip.mp4", faststart=True, edit_list=False, seconds="1")
    data = clip.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    store = LocalDiskStore(tmp_path / "media")
    key = make_media_key("ws1", "a1", digest, "mp4")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id="ws1")
    store.receive_part(plan, 1, data)
    store.complete_upload(plan)

    probe_input = new_probe_input(store.scratch_dir)
    store.copy_to_path(key, probe_input, workspace_id="ws1", sha256=digest, size=len(data))
    try:
        assert prober.probe(probe_input).container == "mp4"
        poster = prober.extract_poster(probe_input, tmp_path / "poster.jpg")
        assert poster.stat().st_size > 0
    finally:
        probe_input.unlink()
    with store.open(key, workspace_id="ws1") as handle:
        assert handle.read() == data


def test_poster_frame_is_a_scaled_jpeg(tmp_path: Path, prober: MediaProber) -> None:
    clip = _clip(tmp_path / "clip.mp4", faststart=True, seconds="2", size="1080x1920")
    poster = prober.extract_poster(
        _as_input(clip, tmp_path), tmp_path / "posters" / "clip.jpg", at_ms=1000, max_width=540
    )
    assert poster.is_file()
    result = prober.probe(_as_input(poster, tmp_path))
    assert (result.kind, result.container, result.width, result.height) == (
        "image",
        "jpeg",
        540,
        960,
    )


def test_unreadable_file_raises_probe_error_without_paths(
    tmp_path: Path, prober: MediaProber
) -> None:
    junk = new_probe_input(tmp_path)
    junk.write_bytes(b"\0" * 1024)
    with pytest.raises(ProbeError) as raised:
        prober.probe(junk)
    assert str(tmp_path) not in str(raised.value)
    assert junk.name not in str(raised.value)
