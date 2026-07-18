from pathlib import Path

from PIL import Image

from runway.config import Settings
from runway.media.service import (
    ensure_fixture_images,
    hamming_similarity,
    inspect_image,
    prepare_model_image,
)


def test_transformed_fixture_remains_visually_similar(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assets = ensure_fixture_images(settings)
    original = inspect_image(assets["history-01"])
    resized = inspect_image(assets["history-01-resized"])
    cropped = inspect_image(assets["history-01-cropped"])
    unrelated = inspect_image(assets["unrelated"])

    assert original.sha256 != resized.sha256
    assert hamming_similarity(original.perceptual_hash, resized.perceptual_hash) >= 0.9
    assert hamming_similarity(original.perceptual_hash, cropped.perceptual_hash) >= 0.75
    assert hamming_similarity(original.perceptual_hash, unrelated.perceptual_hash) < 0.95


def test_animated_gif_becomes_deterministic_model_contact_sheet(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    source = settings.resolved_data_dir / "raw" / "animated.gif"
    source.parent.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("RGB", (120, 80), color) for color in ("red", "green", "blue", "yellow")]
    frames[0].save(
        source,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        format="GIF",
    )

    prepared = prepare_model_image(source, settings)
    repeated = prepare_model_image(source, settings)

    assert prepared == repeated
    assert prepared.suffix == ".png"
    with Image.open(prepared) as contact_sheet:
        assert contact_sheet.size == (360, 160)
        assert getattr(contact_sheet, "n_frames", 1) == 1
