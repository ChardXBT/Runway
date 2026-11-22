"""Probe uploaded media with ffprobe and extract a poster frame with ffmpeg (spec 02 §4).

Security rules this module enforces:

- the input is a :class:`pathlib.Path` to a regular file the server wrote; it is passed as
  ``file:<absolute path>``, so it can never be read as an option or a network URL;
- ``-protocol_whitelist file`` stops the demuxers from opening anything but local files;
- every run has a timeout and no shell;
- the media kind comes from the probe, never from the filename or the client's content type.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from runway.mediastore.mp4atoms import Mp4Layout, read_layout_from_path

MediaKind = Literal["video", "image", "audio", "unknown"]

DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0
DEFAULT_POSTER_MAX_WIDTH = 1280

# ffprobe format names that mean a still image, whatever the file is called.
_IMAGE_FORMATS = frozenset(
    {
        "image2",
        "jpeg_pipe",
        "png_pipe",
        "webp_pipe",
        "bmp_pipe",
        "tiff_pipe",
        "gif",
        "gif_pipe",
        "heif",
        "avif",
        "jpegxl_pipe",
        "qoi_pipe",
    }
)
_IMAGE_CODEC_CONTAINERS = {
    "mjpeg": "jpeg",
    "png": "png",
    "webp": "webp",
    "bmp": "bmp",
    "tiff": "tiff",
    "gif": "gif",
    "hevc": "heif",
    "av1": "avif",
    "jpegxl": "jpegxl",
}
_ISO_FORMATS = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})
_WEBM_VIDEO_CODECS = frozenset({"vp8", "vp9", "av1"})
_WEBM_AUDIO_CODECS = frozenset({"opus", "vorbis"})


class ProbeError(Exception):
    """The file could not be probed. The message is safe to store and show."""


class MediaToolMissing(ProbeError):
    pass


class ProbeTimeout(ProbeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    kind: MediaKind
    container: str | None
    format_names: tuple[str, ...]
    size_bytes: int | None
    duration_ms: int | None
    bitrate: int | None
    video_codec: str | None
    # Display dimensions: rotation metadata already applied.
    width: int | None
    height: int | None
    rotation: int
    fps: float | None
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None
    moov_at_front: bool | None
    has_edit_list: bool | None

    @property
    def aspect_ratio(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width / self.height

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``media_assets_v2.probe_json``."""
        payload = asdict(self)
        payload["format_names"] = list(self.format_names)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProbeResult:
        values = dict(payload)
        values["format_names"] = tuple(values.get("format_names") or ())
        return cls(**values)


@dataclass(frozen=True)
class MediaTools:
    """Where to find the binaries. Defaults look them up on ``PATH``."""

    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS


def _resolve_binary(name_or_path: str, purpose: str) -> str:
    resolved = shutil.which(name_or_path)
    if resolved is None:
        raise MediaToolMissing(
            f"{purpose} binary '{name_or_path}' was not found. Install FFmpeg "
            "(https://ffmpeg.org/download.html) or configure the path to the binary."
        )
    return resolved


def _checked_input(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("media path must be a pathlib.Path the server generated")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ProbeError("media file does not exist")
    return resolved


def _run(arguments: Sequence[str], timeout: float, tool: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeTimeout(f"{tool} did not finish within {timeout:g} seconds") from exc
    except OSError as exc:
        raise MediaToolMissing(f"{tool} could not be started: {exc.strerror or exc}") from exc


def _stderr_line(result: subprocess.CompletedProcess[bytes]) -> str:
    text = result.stderr.decode("utf-8", errors="replace").strip()
    # The last line names the problem; earlier lines can echo the path.
    return text.splitlines()[-1][:300] if text else f"exit code {result.returncode}"


class MediaProber:
    def __init__(self, tools: MediaTools | None = None) -> None:
        self._tools = tools or MediaTools()

    def probe(self, path: Path) -> ProbeResult:
        source = _checked_input(path)
        ffprobe = _resolve_binary(self._tools.ffprobe, "ffprobe")
        result = _run(
            [
                ffprobe,
                "-hide_banner",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                f"file:{source}",
            ],
            self._tools.timeout_seconds,
            "ffprobe",
        )
        if result.returncode != 0:
            raise ProbeError(f"ffprobe could not read the file: {_stderr_line(result)}")
        try:
            payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProbeError("ffprobe returned output that is not JSON") from exc
        if not isinstance(payload, dict):
            raise ProbeError("ffprobe returned output that is not a JSON object")
        layout = read_layout_from_path(source) if _is_iso_media(payload) else None
        return parse_ffprobe(payload, layout=layout, size_bytes=source.stat().st_size)

    def extract_poster(
        self,
        source: Path,
        destination: Path,
        *,
        at_ms: int = 0,
        max_width: int = DEFAULT_POSTER_MAX_WIDTH,
    ) -> Path:
        """Write one JPEG frame from ``source`` (at ``at_ms`` for video) to ``destination``."""
        source_path = _checked_input(source)
        if not isinstance(destination, Path) or destination.suffix.lower() not in {
            ".jpg",
            ".jpeg",
        }:
            raise ProbeError("poster destination must be a .jpg pathlib.Path")
        if at_ms < 0 or max_width <= 0:
            raise ValueError("at_ms must be >= 0 and max_width > 0")
        target = destination.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = _resolve_binary(self._tools.ffmpeg, "ffmpeg")
        seek = ["-ss", f"{at_ms / 1000:.3f}"] if at_ms else []
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                *seek,
                "-i",
                f"file:{source_path}",
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({max_width},iw)':-2",
                "-q:v",
                "3",
                "-f",
                "image2",
                "-y",
                f"file:{target}",
            ],
            self._tools.timeout_seconds,
            "ffmpeg",
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise ProbeError(f"ffmpeg could not extract a poster frame: {_stderr_line(result)}")
        return target


def default_poster_time_ms(probe: ProbeResult) -> int:
    """One second in, or the middle of a clip shorter than two seconds; 0 for images."""
    if probe.kind != "video" or not probe.duration_ms:
        return 0
    return min(1000, probe.duration_ms // 2)


# Parsing ---------------------------------------------------------------------------------


def _format_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = str(payload.get("format", {}).get("format_name") or "")
    return tuple(name for name in raw.split(",") if name)


def _is_iso_media(payload: Mapping[str, Any]) -> bool:
    return bool(_ISO_FORMATS & set(_format_names(payload)))


def _int(value: Any) -> int | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _seconds_to_ms(value: Any) -> int | None:
    if value is None or value == "N/A":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(seconds * 1000) if seconds > 0 else None


def _rate(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return round(float(fraction), 3) if fraction > 0 else None


def _rotation(stream: Mapping[str, Any]) -> int:
    rotation: float | None = None
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, Mapping) and "rotation" in side_data:
            rotation = float(side_data["rotation"])
            break
    if rotation is None:
        rotation = float(_int((stream.get("tags") or {}).get("rotate")) or 0)
    return int(round(rotation / 90.0) * 90) % 360


def _first_stream(
    streams: Sequence[Mapping[str, Any]], codec_type: str
) -> Mapping[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") != codec_type:
            continue
        # Cover art in audio files is a "video" stream with this disposition.
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            continue
        return stream
    return None


def _kind(format_names: tuple[str, ...], video: Mapping[str, Any] | None, audio: Any) -> MediaKind:
    if video is not None and _IMAGE_FORMATS & set(format_names):
        return "image"
    if video is not None:
        return "video"
    if audio is not None:
        return "audio"
    return "unknown"


def _container(
    kind: MediaKind,
    format_names: tuple[str, ...],
    major_brand: str | None,
    video_codec: str | None,
    audio_codec: str | None,
) -> str | None:
    names = set(format_names)
    if kind == "image":
        return _IMAGE_CODEC_CONTAINERS.get(video_codec or "", video_codec)
    if names & _ISO_FORMATS:
        if major_brand is not None and major_brand.strip() == "qt":
            return "mov"
        return "mp4"
    if "matroska" in names or "webm" in names:
        # ffprobe reports both as "matroska,webm"; WebM allows only these codecs.
        video_ok = video_codec is None or video_codec in _WEBM_VIDEO_CODECS
        audio_ok = audio_codec is None or audio_codec in _WEBM_AUDIO_CODECS
        return "webm" if video_ok and audio_ok and video_codec else "matroska"
    return format_names[0] if format_names else None


def parse_ffprobe(
    payload: Mapping[str, Any],
    *,
    layout: Mp4Layout | None = None,
    size_bytes: int | None = None,
) -> ProbeResult:
    """Turn ``ffprobe -print_format json -show_format -show_streams`` output into a result."""
    fmt = payload.get("format") or {}
    streams = [stream for stream in payload.get("streams") or [] if isinstance(stream, Mapping)]
    format_names = _format_names(payload)
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")
    kind = _kind(format_names, video, audio)

    video_codec = str(video["codec_name"]) if video and video.get("codec_name") else None
    audio_codec = str(audio["codec_name"]) if audio and audio.get("codec_name") else None
    rotation = _rotation(video) if video else 0
    width = _int(video.get("width")) if video else None
    height = _int(video.get("height")) if video else None
    if rotation in (90, 270):
        width, height = height, width

    major_brand = layout.major_brand if layout else (fmt.get("tags") or {}).get("major_brand")
    duration_ms = None
    fps = None
    if kind in ("video", "audio"):
        duration_ms = _seconds_to_ms(fmt.get("duration"))
        if duration_ms is None:
            primary = video or audio or {}
            duration_ms = _seconds_to_ms(primary.get("duration"))
    if kind == "video" and video:
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))

    return ProbeResult(
        kind=kind,
        container=_container(kind, format_names, major_brand, video_codec, audio_codec),
        format_names=format_names,
        size_bytes=size_bytes if size_bytes is not None else _int(fmt.get("size")),
        duration_ms=duration_ms,
        bitrate=_int(fmt.get("bit_rate")) if kind != "image" else None,
        video_codec=video_codec,
        width=width,
        height=height,
        rotation=rotation,
        fps=fps,
        audio_codec=audio_codec,
        audio_sample_rate=_int(audio.get("sample_rate")) if audio else None,
        audio_channels=_int(audio.get("channels")) if audio else None,
        moov_at_front=layout.moov_at_front if layout else None,
        has_edit_list=layout.has_edit_list if layout else None,
    )
