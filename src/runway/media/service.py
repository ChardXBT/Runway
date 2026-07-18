from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance
from pydantic import BaseModel

from runway.config import Settings


class ImageFeatures(BaseModel):
    sha256: str
    mime_type: str
    width: int
    height: int
    file_size: int
    perceptual_hash: str
    crop_resistant_hash: str
    embedding_model: str = "runway-local-image-v3"
    embedding: list[float]
    blur_score: float
    quality_metrics: dict[str, float]

    def embedding_bytes(self) -> bytes:
        return np.asarray(self.embedding, dtype=np.float32).tobytes()


def _dct_matrix(size: int) -> np.ndarray:
    x = np.arange(size)
    matrix = np.cos((math.pi / size) * (x[None, :] + 0.5) * np.arange(size)[:, None])
    matrix[0] *= 1 / math.sqrt(2)
    return matrix * math.sqrt(2 / size)


_DCT_32 = _dct_matrix(32)


def _phash(image: Image.Image) -> str:
    gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=float)
    dct = _DCT_32 @ gray @ _DCT_32.T
    low = dct[:8, :8]
    median = float(np.median(low.flatten()[1:]))
    bits = (low > median).flatten()
    return f"{int(''.join('1' if bit else '0' for bit in bits), 2):016x}"


def _crop_descriptor(image: Image.Image) -> tuple[str, list[float]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    crops = [
        rgb,
        rgb.crop((left, top, left + side, top + side)),
        rgb.crop((0, 0, max(side, width * 3 // 4), max(side, height * 3 // 4))),
    ]
    descriptors: list[float] = []
    for crop in crops:
        small = np.asarray(crop.resize((4, 4), Image.Resampling.LANCZOS), dtype=np.float32)
        means = small.reshape(-1, 3).mean(axis=0) / 255.0
        stds = small.reshape(-1, 3).std(axis=0) / 255.0
        descriptors.extend([*means.tolist(), *stds.tolist()])
    vector = np.asarray(descriptors, dtype=np.float32)
    norm = float(np.linalg.norm(vector)) or 1.0
    normalized = (vector / norm).tolist()
    return json.dumps([round(float(value), 7) for value in normalized]), normalized


def _embedding(image: Image.Image) -> list[float]:
    rgb = image.convert("RGB")
    inset_x = max(1, round(rgb.width * 0.08))
    inset_y = max(1, round(rgb.height * 0.08))
    rgb = rgb.crop((inset_x, inset_y, rgb.width - inset_x, rgb.height - inset_y))
    thumbnail = np.asarray(rgb.resize((8, 8), Image.Resampling.LANCZOS), dtype=np.float32)
    gray = thumbnail.mean(axis=2).flatten() / 255.0
    histogram_parts: list[np.ndarray] = []
    array = np.asarray(rgb.resize((128, 128), Image.Resampling.LANCZOS), dtype=np.uint8)
    for channel in range(3):
        hist, _ = np.histogram(array[:, :, channel], bins=8, range=(0, 256), density=True)
        histogram_parts.append(hist.astype(np.float32))
    edge_h = np.abs(np.diff(gray.reshape(8, 8), axis=1)).mean(axis=1)
    edge_v = np.abs(np.diff(gray.reshape(8, 8), axis=0)).mean(axis=0)
    vector = np.concatenate([gray, *histogram_parts, edge_h, edge_v]).astype(np.float32)
    norm = float(np.linalg.norm(vector)) or 1.0
    return [float(value) for value in vector / norm]


def inspect_image(path: Path) -> ImageFeatures:
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    with Image.open(path) as opened:
        opened.load()
        image = opened.convert("RGB")
        width, height = image.size
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        if min(gray.shape) > 2:
            laplacian = (
                -4 * gray[1:-1, 1:-1]
                + gray[:-2, 1:-1]
                + gray[2:, 1:-1]
                + gray[1:-1, :-2]
                + gray[1:-1, 2:]
            )
            blur_score = float(np.var(laplacian))
        else:
            blur_score = 0.0
        crop_hash, _crop_vector = _crop_descriptor(image)
        mime_type = Image.MIME.get(opened.format or "", "application/octet-stream")
        pixels = max(width * height, 1)
        quality_metrics = {
            "aspect_ratio": round(width / max(height, 1), 6),
            "blur_score": round(blur_score, 6),
            "bytes_per_pixel": round(len(raw) / pixels, 6),
            "luminance_variance": round(float(np.var(gray)), 6),
        }
        return ImageFeatures(
            sha256=sha256,
            mime_type=mime_type,
            width=width,
            height=height,
            file_size=len(raw),
            perceptual_hash=_phash(image),
            crop_resistant_hash=crop_hash,
            embedding=_embedding(image),
            blur_score=blur_score,
            quality_metrics=quality_metrics,
        )


def hamming_similarity(first: str, second: str) -> float:
    width = max(len(first), len(second)) * 4
    if not first or not second or width == 0:
        return 0.0
    distance = (int(first, 16) ^ int(second, 16)).bit_count()
    return 1.0 - (distance / width)


def cosine_similarity(first: bytes | list[float], second: bytes | list[float]) -> float:
    a = (
        np.frombuffer(first, dtype=np.float32)
        if isinstance(first, bytes)
        else np.asarray(first, dtype=np.float32)
    )
    b = (
        np.frombuffer(second, dtype=np.float32)
        if isinstance(second, bytes)
        else np.asarray(second, dtype=np.float32)
    )
    if a.shape != b.shape or not a.size:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def crop_descriptor_similarity(first: str | None, second: str | None) -> float:
    if not first or not second:
        return 0.0
    try:
        first_values = json.loads(first)
        second_values = json.loads(second)
    except (json.JSONDecodeError, TypeError):
        return 1.0 if first == second else 0.0
    if not isinstance(first_values, list) or not isinstance(second_values, list):
        return 0.0
    return cosine_similarity(
        [float(value) for value in first_values],
        [float(value) for value in second_values],
    )


def content_addressed_copy(
    source: Path,
    settings: Settings,
    kind: str,
    features: ImageFeatures | None = None,
) -> tuple[Path, str]:
    inspected = features or inspect_image(source)
    extension = {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(inspected.mime_type, source.suffix.lower() or ".bin")
    relative = Path("media") / kind / f"{inspected.sha256}{extension}"
    destination = settings.resolved_data_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
    return destination, relative.as_posix()


def create_square_preview(source: Path, destination: Path, size: int = 640) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        width, height = image.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        preview = image.crop((left, top, left + side, top + side))
        preview = preview.resize((size, size), Image.Resampling.LANCZOS)
        preview.save(destination, format="JPEG", quality=90, optimize=True)
    return destination


def prepare_model_image(
    source: Path,
    settings: Settings,
    *,
    maximum_frames: int = 6,
    maximum_frame_dimension: int = 512,
) -> Path:
    """Return a model-compatible still or deterministic contact sheet for animation."""
    supported_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    with Image.open(source) as opened:
        frame_count = int(getattr(opened, "n_frames", 1))
        if source.suffix.lower() in supported_suffixes and frame_count <= 1:
            return source

        selected_count = min(maximum_frames, frame_count)
        if selected_count <= 1:
            frame_indices = [0]
        else:
            frame_indices = [
                round(index * (frame_count - 1) / (selected_count - 1))
                for index in range(selected_count)
            ]
        frames: list[Image.Image] = []
        for frame_index in frame_indices:
            opened.seek(frame_index)
            frame = opened.convert("RGB")
            frame.thumbnail(
                (maximum_frame_dimension, maximum_frame_dimension),
                Image.Resampling.LANCZOS,
            )
            frames.append(frame.copy())

    columns = min(3, len(frames))
    rows = math.ceil(len(frames) / columns)
    cell_width = max(frame.width for frame in frames)
    cell_height = max(frame.height for frame in frames)
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "black")
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        column = index % columns
        row = index // columns
        left = column * cell_width + (cell_width - frame.width) // 2
        top = row * cell_height + (cell_height - frame.height) // 2
        sheet.paste(frame, (left, top))
        if len(frames) > 1:
            draw.rectangle((left, top, left + 34, top + 18), fill="black")
            draw.text((left + 4, top + 2), str(index + 1), fill="white")

    destination = (
        settings.resolved_data_dir / "media" / "previews" / "model-inputs" / f"{source.stem}.png"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        sheet.save(destination, format="PNG", optimize=True)
    return destination


def _fixture_image(path: Path, index: int, candidate: bool) -> None:
    width = 720 + (index % 3) * 80
    height = 540 + (index % 4) * 40
    base_hue = (index * 37 + (71 if candidate else 0)) % 255
    image = Image.new("RGB", (width, height), (28 + base_hue // 5, 52, 68))
    draw = ImageDraw.Draw(image)
    if candidate:
        for step in range(9):
            x = (step * 97 + index * 29) % width
            color = (
                (base_hue + step * 23) % 255,
                (120 + index * 13 + step * 17) % 255,
                (60 + index * 19 + step * 29) % 255,
            )
            draw.line((x - 180, 0, x + 120, height), fill=color, width=24)
        for subject in range(1 + index % 3):
            radius = min(width, height) // (8 + subject)
            center_x = width * (subject + 1) // (2 + index % 3)
            center_y = height // 2 + ((index + subject) % 3 - 1) * 70
            draw.ellipse(
                (
                    center_x - radius,
                    center_y - radius,
                    center_x + radius,
                    center_y + radius,
                ),
                fill=((base_hue + subject * 70) % 255, (index * 31) % 255, 205),
                outline="white",
                width=7,
            )
    else:
        for step in range(12):
            inset = 18 + step * 17
            color = (
                (base_hue + step * 13) % 255,
                (80 + index * 17 + step * 7) % 255,
                (150 + index * 9 + step * 11) % 255,
            )
            draw.rounded_rectangle(
                (inset, inset, width - inset, height - inset),
                radius=18 + step,
                outline=color,
                width=8,
            )
        radius = min(width, height) // 7
        center_x = width // 2 + ((index % 5) - 2) * 33
        center_y = height // 2 + ((index % 3) - 1) * 31
        draw.ellipse(
            (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
            fill=((base_hue + 90) % 255, (index * 29) % 255, 190),
        )
    draw.text(
        (28, height - 48),
        f"SYNTHETIC {'CANDIDATE' if candidate else 'HISTORY'} {index:02d}",
        fill="white",
    )
    image.save(path, format="JPEG", quality=92, optimize=True)


def ensure_fixture_images(settings: Settings) -> dict[str, Path]:
    root = settings.resolved_data_dir / "raw" / "fixture-images"
    root.mkdir(parents=True, exist_ok=True)
    version_path = root / "generator-version.txt"
    regenerate = not version_path.is_file() or version_path.read_text(encoding="utf-8") != "2\n"
    assets: dict[str, Path] = {}
    for candidate, count in ((False, 14), (True, 36)):
        prefix = "candidate" if candidate else "history"
        for index in range(1, count + 1):
            name = f"{prefix}-{index:02d}"
            path = root / f"{name}.jpg"
            if regenerate or not path.exists():
                _fixture_image(path, index, candidate)
            assets[name] = path
    version_path.write_text("2\n", encoding="utf-8")

    # Deterministic transformed fixtures used by duplicate-threshold tests.
    original = assets["history-01"]
    with Image.open(original) as opened:
        resized = opened.resize((480, 360), Image.Resampling.LANCZOS)
        resized_path = root / "history-01-resized.jpg"
        resized.save(resized_path, format="JPEG", quality=72)
        assets["history-01-resized"] = resized_path

        crop = opened.crop((45, 30, opened.width - 45, opened.height - 30))
        crop_path = root / "history-01-cropped.jpg"
        crop.save(crop_path, format="JPEG", quality=84)
        assets["history-01-cropped"] = crop_path

        adjusted = ImageEnhance.Color(opened).enhance(0.55)
        adjusted_path = root / "history-01-color.jpg"
        adjusted.save(adjusted_path, format="JPEG", quality=86)
        assets["history-01-color"] = adjusted_path

        bordered = Image.new("RGB", (opened.width + 80, opened.height + 80), "#f3efe5")
        bordered.paste(opened.convert("RGB"), (40, 40))
        bordered_path = root / "history-01-bordered.jpg"
        bordered.save(bordered_path, format="JPEG", quality=88)
        assets["history-01-bordered"] = bordered_path

    unrelated_path = root / "unrelated-checkerboard.jpg"
    if not unrelated_path.exists():
        unrelated = Image.new("RGB", (760, 560), "#080b0d")
        unrelated_draw = ImageDraw.Draw(unrelated)
        tile = 40
        for y in range(0, unrelated.height, tile):
            for x in range(0, unrelated.width, tile):
                if (x // tile + y // tile) % 2:
                    unrelated_draw.rectangle((x, y, x + tile, y + tile), fill=(245, 235, 45))
        unrelated.save(unrelated_path, format="JPEG", quality=95)
    assets["unrelated"] = unrelated_path

    (root / "manifest.json").write_text(
        json.dumps({name: path.name for name, path in sorted(assets.items())}, indent=2),
        encoding="utf-8",
    )
    return assets
