from pathlib import Path

from leeway.config import Settings
from leeway.media.service import ensure_fixture_images, hamming_similarity, inspect_image


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
