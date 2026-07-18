from runway.analysis.schemas import CandidateAnalysis
from runway.media.service import ImageFeatures
from runway.ranking.duplicates import DuplicateResult
from runway.ranking.service import CandidateRanker


def test_fan_art_is_hard_rejected(database, settings) -> None:
    features = ImageFeatures(
        sha256="a" * 64,
        mime_type="image/png",
        width=1080,
        height=1080,
        file_size=100_000,
        perceptual_hash="0" * 16,
        crop_resistant_hash="[]",
        embedding=[1.0, 0.0],
        blur_score=100.0,
        quality_metrics={"bytes_per_pixel": 0.1},
    )
    analysis = CandidateAnalysis(
        franchise="Fixture Show",
        characters=[],
        scene_archetype="reaction",
        composition="centered",
        emotion="surprise",
        text_overlay=False,
        watermark_probability=0.0,
        unsafe_probability=0.0,
        personal_artwork_probability=0.1,
        fan_art_probability=0.8,
        caption_potential=0.8,
        confidence=0.9,
    )
    duplicate = DuplicateResult(
        is_exact=False,
        is_transformed_duplicate=False,
        highest_visual_similarity=0.0,
        highest_semantic_similarity=0.0,
        closest_asset_ids=[],
        within_180_day_window=False,
        hard_block=False,
        warnings=[],
    )

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="example.test",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason == "fan_art"
    assert result.final_rank_score == 0.0
    assert "possible fan art" in result.warnings
