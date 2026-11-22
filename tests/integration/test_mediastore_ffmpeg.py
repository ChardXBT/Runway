"""Probe and poster extraction against the real ffmpeg binaries, on tiny generated clips.

Skipped when ffmpeg or ffprobe is not on PATH. Clips are generated with ``-f lavfi`` into
``tmp_path``; nothing is downloaded and no video is committed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from runway.mediastore import MediaProber, validate

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


def _clip(path: Path, *, faststart: bool, seconds: str = "3.5", size: str = "360x640") -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", seconds, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2",
        *(["-movflags", "+faststart"] if faststart else []),
        str(path),
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def prober() -> MediaProber:
    return MediaProber()


def test_faststart_clip_is_probed_and_valid_for_every_video_target(
    tmp_path: Path, prober: MediaProber
) -> None:
    clip = _clip(tmp_path / "-starts with dash & space.mp4", faststart=True)
    result = prober.probe(clip)
    assert (result.kind, result.container, result.video_codec, result.audio_codec) == (
        "video",
        "mp4",
        "h264",
        "aac",
    )
    assert (result.width, result.height, result.fps) == (360, 640, 30.0)
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


def test_moov_at_end_is_detected_and_the_named_fix_works(
    tmp_path: Path, prober: MediaProber
) -> None:
    clip = _clip(tmp_path / "late-moov.mp4", faststart=False)
    result = prober.probe(clip)
    assert result.moov_at_front is False
    [issue] = [i for i in validate(result, "instagram", "instagram_reel") if i.severity == "error"]
    assert issue.code == "moov_not_at_front" and issue.fix is not None

    fixed = tmp_path / "fixed.mp4"
    _ffmpeg("-i", str(clip), "-c", "copy", "-movflags", "+faststart", str(fixed))
    assert prober.probe(fixed).moov_at_front is True


def test_type_is_decided_by_content_not_extension(tmp_path: Path, prober: MediaProber) -> None:
    jpeg = tmp_path / "photo.jpg"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1350", "-frames:v", "1", str(jpeg))
    disguised = tmp_path / "photo.mp4"
    shutil.copyfile(jpeg, disguised)
    result = prober.probe(disguised)
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
    disguised_video = tmp_path / "clip.mov"
    shutil.copyfile(webm, disguised_video)
    probed = prober.probe(disguised_video)
    assert (probed.kind, probed.container, probed.moov_at_front) == ("video", "webm", None)


def test_poster_frame_is_a_scaled_jpeg(tmp_path: Path, prober: MediaProber) -> None:
    clip = _clip(tmp_path / "clip.mp4", faststart=True, seconds="2", size="1080x1920")
    poster = prober.extract_poster(
        clip, tmp_path / "posters" / "clip.jpg", at_ms=1000, max_width=540
    )
    assert poster.is_file()
    result = prober.probe(poster)
    assert (result.kind, result.container, result.width, result.height) == (
        "image",
        "jpeg",
        540,
        960,
    )


def test_unreadable_file_raises_probe_error(tmp_path: Path, prober: MediaProber) -> None:
    from runway.mediastore import ProbeError

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\0" * 1024)
    with pytest.raises(ProbeError):
        prober.probe(junk)
