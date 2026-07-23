import json
from datetime import UTC, datetime

from runway.analysis.schemas import CandidateAnalysis
from runway.db.models import StyleProfile
from runway.db.repositories import get_channel
from runway.media.service import ImageFeatures
from runway.ranking.duplicates import DuplicateResult
from runway.ranking.service import CandidateRanker


def test_fan_art_is_allowed_and_preserved_as_metadata(database, settings) -> None:
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
        entities=[],
        objects=[],
        actions=[],
        relationships=[],
        setting="unknown",
        ocr_text=[],
        field_confidence={
            "entities": 0.9,
            "emotion": 0.9,
            "actions": 0.9,
            "scene": 0.9,
            "ocr": 0.9,
        },
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

    assert result.hard_rejection_reason is None
    assert result.final_rank_score > 0.0
    assert "possible fan art" not in result.warnings


def test_artist_portfolio_domain_is_allowed_unless_creator_explicitly_blocks_it(
    database, settings
) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="The Simpsons")

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="www.deviantart.com",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason is None
    assert result.final_rank_score > 0.0


def test_obvious_merchandise_photo_is_not_treated_as_a_cartoon_frame(
    database,
    settings,
) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="Family Guy")
    analysis = analysis.model_copy(
        update={
            "scene_archetype": "product or collection photograph",
            "composition": "top-down photograph of a display",
            "objects": ["six plush dolls", "product tags", "carpet"],
            "setting": "indoor carpeted floor",
        }
    )

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="familyguy.fandom.com",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason == "non_frame_merchandise"
    assert result.final_rank_score == 0.0


def test_intrusive_production_credit_overlay_is_rejected(
    database,
    settings,
) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="The Simpsons")
    analysis = analysis.model_copy(
        update={
            "text_overlay": True,
            "ocr_text": ["CO-EXECUTIVE PRODUCER ROB LAZEBNIK"],
        }
    )

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="frinkiac.com",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason == "intrusive_production_credit"
    assert result.final_rank_score == 0.0


def test_standalone_production_credit_title_card_is_rejected(
    database,
    settings,
) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="The Simpsons")
    analysis = analysis.model_copy(
        update={
            "characters": [],
            "scene_archetype": "Credit/title card",
            "text_overlay": True,
            "ocr_text": ["JULIE KAVNER"],
            "caption_potential": 0.12,
        }
    )

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="frinkiac.com",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason == "production_credit_title_card"
    assert result.final_rank_score == 0.0


def test_preflight_rejection_cannot_be_lost_during_ranking(
    database,
    settings,
) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="The Simpsons")

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="frinkiac.com",
        rights_status="unknown",
        preflight_rejection="adjacent_archive_frame",
    )

    assert result.hard_rejection_reason == "adjacent_archive_frame"
    assert result.final_rank_score == 0.0


def test_nsfw_candidate_is_always_hard_rejected(database, settings) -> None:
    features, analysis, duplicate = _rank_inputs(franchise="The Simpsons")
    analysis = analysis.model_copy(update={"unsafe_probability": 0.91})

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="example.test",
        rights_status="public_domain",
    )

    assert result.hard_rejection_reason == "nsfw_content"
    assert result.final_rank_score == 0.0


def test_unrelated_candidate_is_rejected_against_active_franchise_profile(
    database, settings
) -> None:
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        session.add(
            StyleProfile(
                channel_id=channel.id,
                version=1,
                catalogue_cutoff=datetime.now(UTC),
                profile_json=json.dumps(
                    {
                        "caption_statistics": {"sample_size": 200},
                        "franchise_distribution": [
                            ["The Simpsons", 196],
                            ["unknown", 4],
                        ],
                    }
                ),
                representative_post_ids_json="[]",
                excluded_post_ids_json="[]",
                is_active=True,
            )
        )
    features, analysis, duplicate = _rank_inputs(franchise=None)

    result = CandidateRanker(database, settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="example.test",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason == "off_topic"
    assert result.final_rank_score == 0.0


def test_creator_approved_secondary_franchise_is_supported(
    database,
    settings,
) -> None:
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        session.add(
            StyleProfile(
                channel_id=channel.id,
                version=1,
                catalogue_cutoff=datetime.now(UTC),
                profile_json=json.dumps(
                    {
                        "caption_statistics": {"sample_size": 200},
                        "franchise_distribution": [["The Simpsons", 200]],
                    }
                ),
                representative_post_ids_json="[]",
                excluded_post_ids_json="[]",
                is_active=True,
            )
        )
    mixed_settings = settings.model_copy(
        update={"discovery_secondary_topics": "Family Guy,Futurama"}
    )
    features, analysis, duplicate = _rank_inputs(franchise="Family Guy")

    result = CandidateRanker(database, mixed_settings).rank(
        features,
        analysis,
        duplicate,
        source_domain="familyguy.fandom.com",
        rights_status="unknown",
    )

    assert result.hard_rejection_reason is None
    assert result.topic_eligibility_class == "supported"


def _rank_inputs(
    *, franchise: str | None
) -> tuple[ImageFeatures, CandidateAnalysis, DuplicateResult]:
    return (
        ImageFeatures(
            sha256="b" * 64,
            mime_type="image/jpeg",
            width=1280,
            height=720,
            file_size=200_000,
            perceptual_hash="1" * 16,
            crop_resistant_hash="[]",
            embedding=[1.0, 0.0],
            blur_score=120.0,
            quality_metrics={"bytes_per_pixel": 0.1},
        ),
        CandidateAnalysis(
            franchise=franchise,
            characters=[],
            scene_archetype="reaction",
            composition="centered",
            emotion="surprise",
            text_overlay=False,
            watermark_probability=0.0,
            unsafe_probability=0.0,
            personal_artwork_probability=0.01,
            fan_art_probability=0.01,
            caption_potential=0.8,
            confidence=0.9,
            entities=[],
            objects=[],
            actions=[],
            relationships=[],
            setting="unknown",
            ocr_text=[],
            field_confidence={
                "entities": 0.9,
                "emotion": 0.9,
                "actions": 0.9,
                "scene": 0.9,
                "ocr": 0.9,
            },
        ),
        DuplicateResult(
            is_exact=False,
            is_transformed_duplicate=False,
            highest_visual_similarity=0.0,
            highest_semantic_similarity=0.0,
            closest_asset_ids=[],
            within_180_day_window=False,
            hard_block=False,
            warnings=[],
        ),
    )
