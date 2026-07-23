import json
from datetime import UTC, datetime, timedelta

import pytest

from runway.analysis.schemas import SearchPlan, SearchQueryFamily
from runway.analysis.service import AnalysisService
from runway.captions.service import CaptionService
from runway.captions.visual_consensus import candidate_analysis_from_mapping
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import SearchRun
from runway.db.repositories import get_channel
from runway.discovery.providers import (
    BrowserSearchProvider,
    DuckDuckGoSearchProvider,
    FamilyGuyWikiSearchProvider,
    FrinkiacSearchProvider,
    ManualUrlProvider,
    MorbotronSearchProvider,
    _diverse_queries,
)
from runway.discovery.schemas import ImageSearchResult
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService


@pytest.mark.asyncio
async def test_offline_discovery_preserves_provenance_scores_and_captions(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()

    discovery = DiscoveryService(database, settings)
    result = await discovery.discover(days=10, provider_name="fixture", dry_run=True)
    assert result["candidates"] == 30
    assert result["accepted"] >= 10
    candidates = discovery.list_candidates(run_id=int(result["run_id"]), accepted_only=True)
    assert candidates
    top = candidates[0]
    assert top["source_page_url"].startswith("https://fixture.local/")
    assert top["direct_image_url"].startswith("fixture://candidate-")
    assert top["preview_url"]
    assert set(top["scores"]) >= {
        "quality_score",
        "style_score",
        "novelty_score",
        "caption_potential_score",
        "final_rank_score",
    }

    captions = await CaptionService(database, settings).generate(int(top["id"]))
    options = [captions.recommended, *captions.alternatives]
    assert len(options) == 3
    assert len(set(options)) == 3
    assert captions.recommended.lower().startswith(("why ", "how ", "what "))
    assert captions.recommended.endswith("?")
    historical = {
        "That look when the plan actually works.",
        "Would you trust this plan?",
    }
    assert not historical.intersection(options)
    assert len(captions.referenced_historical_post_ids) <= 8


@pytest.mark.asyncio
async def test_manual_provider_rejects_non_http_urls() -> None:
    from runway.analysis.schemas import SearchPlan

    provider = ManualUrlProvider(["file:///secret.jpg"])
    with pytest.raises(ValueError, match="HTTP"):
        await provider.search(
            SearchPlan(
                query_families=[],
                desired_visual_traits=[],
                excluded_concepts=[],
                desired_entities=[],
                desired_topics=[],
                desired_actions=[],
                desired_scenes=[],
                desired_compositions=[],
                source_policy="preserve_and_review",
                rights_policy="unknown_requires_review",
            )
        )


def test_search_plan_uses_supported_named_franchises() -> None:
    payload = DiscoveryService._plan_payload(
        {
            "version": 3,
            "caption_statistics": {"sample_size": 200},
            "franchise_distribution": [
                ["The Simpsons", 196],
                ["unknown", 2],
                ["Family Guy", 1],
                ["Futurama", 1],
            ],
            "visual_compositions": [["close-up", 20]],
            "visual_formats": [["animated still or frame", 190]],
            "dominant_caption_structures": [["short phrase", 80]],
        },
        1,
    )
    assert payload["primary_franchise"] == "The Simpsons"
    assert payload["underused_franchises"] == ["The Simpsons"]
    assert payload["primary_topic_share"] == pytest.approx(196 / 198)
    assert payload["minimum_primary_topic_query_share"] == 0.8
    assert payload["recent_exclusions"] == []


def test_search_plan_honours_explicit_creator_secondary_topics() -> None:
    payload = DiscoveryService._plan_payload(
        {
            "version": 3,
            "caption_statistics": {"sample_size": 200},
            "topic_distribution": [["The Simpsons", 196], ["Family Guy", 1]],
        },
        120,
        recent_search_queries=[
            "The Simpsons Homer fishing at a lake screencap",
            "Family Guy Peter Griffin kitchen animated frame",
        ],
        primary_topic_query_share=0.67,
        explicit_secondary_topics=["Family Guy", "Futurama", "The Simpsons"],
    )

    assert payload["primary_topic"] == "The Simpsons"
    assert payload["minimum_primary_topic_query_share"] == 0.67
    assert payload["explicit_secondary_topics"] == ["Family Guy", "Futurama"]
    assert payload["underused_topics"] == ["Family Guy", "Futurama"]
    assert payload["recent_search_queries"] == [
        "The Simpsons Homer fishing at a lake screencap",
        "Family Guy Peter Griffin kitchen animated frame",
    ]
    assert payload["topic_query_targets"] == {
        "total_queries": 6,
        "primary_topic_queries": 4,
        "secondary_topic_queries": 2,
    }


def test_exact_archive_provenance_fills_only_a_missing_franchise_label() -> None:
    missing = candidate_analysis_from_mapping(
        {
            "franchise": None,
            "characters": ["an unidentified cartoon man"],
            "confidence": 0.9,
        }
    )
    contradicted = missing.model_copy(update={"franchise": "A Different Show"})
    result = ImageSearchResult(
        search_query="Family Guy animated frame",
        result_rank=1,
        source_page_url="https://familyguy.fandom.com/wiki/File:Frame.png",
        direct_image_url="https://static.wikia.nocookie.net/frame.png",
        source_domain="familyguy.fandom.com",
        original_width=1280,
        original_height=720,
        rights_status="unknown",
        provider_metadata={
            "source_adapter": "family-guy-fandom-mediawiki",
        },
    )

    enriched = DiscoveryService._apply_trusted_source_franchise(missing, result)
    preserved = DiscoveryService._apply_trusted_source_franchise(contradicted, result)

    assert enriched.franchise == "Family Guy"
    assert preserved.franchise == "A Different Show"


def test_archive_frame_identity_requires_exact_timestamped_provider_metadata() -> None:
    valid = ImageSearchResult(
        search_query="Futurama Bender frame",
        result_rank=1,
        source_page_url="https://morbotron.com/caption/S06E01/1137553",
        direct_image_url="https://morbotron.com/img/S06E01/1137553.jpg",
        source_domain="morbotron.com",
        original_width=1280,
        original_height=720,
        rights_status="unknown",
        provider_metadata={
            "source_adapter": "morbotron-public-search",
            "episode": "S06E01",
            "timestamp": 1137553,
        },
    )
    untrusted = valid.model_copy(
        update={
            "provider_metadata": {
                "source_adapter": "family-guy-fandom-mediawiki",
                "episode": "S06E01",
                "timestamp": 1137553,
            }
        }
    )

    assert DiscoveryService._archive_frame_identity(valid) == (
        "morbotron-public-search",
        "S06E01",
        1137553,
    )
    assert DiscoveryService._archive_frame_identity(untrusted) is None


def test_archive_scene_window_blocks_visually_repeated_shots_nine_seconds_apart() -> None:
    assert DiscoveryService._timestamps_share_archive_scene(1_222_430, 1_231_397)
    assert not DiscoveryService._timestamps_share_archive_scene(1_222_430, 1_242_431)


def test_search_plan_novelty_uses_effective_archive_queries_not_surface_wording() -> None:
    plan = SearchPlan(
        query_families=[
            SearchQueryFamily(
                purpose="test",
                queries=[
                    "The Simpsons Bart Simpson discovering a hidden note in school screencap",
                    "Futurama Leela serving drinks at a spaceport bar frame",
                ],
            )
        ],
        desired_visual_traits=[],
        excluded_concepts=[],
        desired_entities=[],
        desired_topics=["The Simpsons", "Futurama"],
        desired_actions=[],
        desired_scenes=[],
        desired_compositions=[],
        source_policy="public",
        rights_policy="preserve",
    )

    repeated = DiscoveryService._repeated_search_queries(
        plan,
        [
            "The Simpsons Bart Simpson examining a message in a hallway frame",
            "Futurama Leela serving drinks at a crowded spaceport bar frame",
        ],
    )

    assert repeated == [
        "The Simpsons Bart Simpson discovering a hidden note in school screencap",
        "Futurama Leela serving drinks at a spaceport bar frame",
    ]


def test_search_plan_repairs_adapter_level_repeats_without_changing_topic_mix() -> None:
    plan = SearchPlan(
        query_families=[
            SearchQueryFamily(
                purpose="primary",
                queries=[
                    "The Simpsons Marge Simpson comforting Maggie in a storm frame",
                    "The Simpsons Lisa Simpson playing chess at home frame",
                ],
            ),
            SearchQueryFamily(
                purpose="secondary",
                queries=[
                    "Family Guy Stewie Griffin playing a board game frame",
                    "Futurama Amy Wong delivering a package frame",
                ],
            ),
        ],
        desired_visual_traits=[],
        excluded_concepts=[],
        desired_entities=[],
        desired_topics=["The Simpsons", "Family Guy", "Futurama"],
        desired_actions=[],
        desired_scenes=[],
        desired_compositions=[],
        source_policy="public",
        rights_policy="preserve",
    )
    recent = [
        "The Simpsons Marge Simpson driving in rain frame",
        "Family Guy Stewie Griffin playing a board game screenshot",
        "Futurama Amy Wong in the office frame",
    ]

    repaired, repairs = DiscoveryService._repair_repeated_archive_queries(plan, recent)

    flattened = [query for family in repaired.query_families for query in family.queries]
    assert len(repairs) == 3
    assert DiscoveryService._repeated_search_queries(repaired, recent) == []
    assert sum("simpson" in query.casefold() for query in flattened) == 2
    assert sum("family guy" in query.casefold() for query in flattened) == 1
    assert sum("futurama" in query.casefold() for query in flattened) == 1
    assert plan.query_families[0].queries[0].startswith("The Simpsons Marge")


def test_discovery_recovers_abandoned_runs_without_touching_active_ones(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        stale = SearchRun(
            channel_id=channel_id,
            style_profile_id=None,
            query_plan_json="{}",
            provider="archives",
            status="running",
            started_at=datetime.now(UTC) - timedelta(hours=7),
        )
        active = SearchRun(
            channel_id=channel_id,
            style_profile_id=None,
            query_plan_json="{}",
            provider="archives",
            status="running",
            started_at=datetime.now(UTC),
        )
        session.add_all([stale, active])
        session.flush()
        stale_id = stale.id
        active_id = active.id

    assert DiscoveryService(database, settings)._recover_stale_runs() == 1

    with database.session() as session:
        stale = session.get(SearchRun, stale_id)
        active = session.get(SearchRun, active_id)
        assert stale is not None
        assert stale.status == "failed"
        assert stale.completed_at is not None
        assert "Recovered abandoned" in str(stale.error_summary)
        assert active is not None
        assert active.status == "running"
        assert active.completed_at is None


def test_recent_search_queries_are_newest_first_deduplicated_and_bounded(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        for query in ("older search", "newer search"):
            session.add(
                SearchRun(
                    channel_id=channel_id,
                    style_profile_id=None,
                    query_plan_json=json.dumps(
                        {
                            "plan": {
                                "query_families": [
                                    {
                                        "purpose": "test",
                                        "queries": [query, "shared search"],
                                    }
                                ]
                            }
                        }
                    ),
                    provider="archives",
                    status="completed",
                )
            )

    queries = DiscoveryService(database, settings)._recent_search_queries(limit=2)

    assert queries == ["newer search", "shared search"]


def test_bing_metadata_parser_preserves_direct_and_source_urls() -> None:
    parsed = BrowserSearchProvider._parse_bing_metadata(
        [
            json.dumps(
                {
                    "murl": "https://images.example.test/frame.jpg",
                    "purl": "https://source.example.test/article",
                    "ow": 1280,
                    "oh": 720,
                    "t": "The Simpsons frame",
                }
            ),
            "{not-json",
            json.dumps({"murl": "data:image/png;base64,abc"}),
        ]
    )
    assert parsed == [
        {
            "src": "https://images.example.test/frame.jpg",
            "page": "https://source.example.test/article",
            "width": 1280,
            "height": 720,
            "index": 0,
            "adapter": "bing-metadata",
            "title": "The Simpsons frame",
        }
    ]


def test_live_browser_queries_rotate_across_families_before_repeating() -> None:
    plan = SearchPlan(
        query_families=[
            SearchQueryFamily(purpose="reactions", queries=["reaction one", "reaction two"]),
            SearchQueryFamily(purpose="groups", queries=["group one", "group two"]),
            SearchQueryFamily(purpose="objects", queries=["object one"]),
        ],
        desired_visual_traits=[],
        excluded_concepts=[],
        desired_entities=[],
        desired_topics=[],
        desired_actions=[],
        desired_scenes=[],
        desired_compositions=[],
        source_policy="preserve_and_review",
        rights_policy="unknown_requires_review",
    )

    assert _diverse_queries(plan) == [
        "reaction one",
        "group one",
        "object one",
        "reaction two",
        "group two",
    ]


def test_frinkiac_query_compaction_keeps_character_and_action() -> None:
    assert (
        FrinkiacSearchProvider._compact_query(
            '"Homer Simpson" quietly fishing lake animated screencap'
        )
        == "Homer fishing"
    )
    assert (
        FrinkiacSearchProvider._compact_query('"Lisa Simpson" performing saxophone stage frame')
        == "Lisa saxophone"
    )
    assert FrinkiacSearchProvider._spread_sample(list(range(10)), 3) == [0, 4, 9]
    assert FrinkiacSearchProvider._spread_sample(
        list(range(10)),
        3,
        offset=1,
    ) == [3, 7, 2]
    assert FrinkiacSearchProvider._supports_query(
        'The Simpsons "Homer Simpson" fishing lake screencap'
    )
    assert not FrinkiacSearchProvider._supports_query('Futurama "Bender" bending factory screencap')
    assert MorbotronSearchProvider._supports_query('Futurama "Bender" bending factory screencap')
    assert not MorbotronSearchProvider._supports_query(
        'Family Guy "Peter Griffin" kitchen screencap'
    )


def test_family_guy_archive_skips_video_and_low_resolution_rows() -> None:
    usable = FamilyGuyWikiSearchProvider._is_usable_image_info

    assert usable("image/png", 1280, 720, minimum_dimension=480)
    assert usable("IMAGE/JPEG", 686, 592, minimum_dimension=480)
    assert not usable("video/youtube", 1920, 1080, minimum_dimension=480)
    assert not usable("image/gif", 1280, 720, minimum_dimension=480)
    assert not usable("image/jpeg", 480, 360, minimum_dimension=480)
    assert not usable("image/webp", None, 720, minimum_dimension=480)


def test_family_guy_archive_rotates_disjoint_screenshot_category_windows() -> None:
    settings = Settings(browser_search_max_queries=3)

    first = FamilyGuyWikiSearchProvider(settings, sample_offset=2)
    second = FamilyGuyWikiSearchProvider(settings, sample_offset=3)

    assert first._rotated_category_prefixes() == ["G", "H", "I"]
    assert second._rotated_category_prefixes() == ["J", "K", "L"]
    assert set(first._rotated_category_prefixes()).isdisjoint(second._rotated_category_prefixes())


def test_keyless_search_balances_primary_and_secondary_topics() -> None:
    provider = DuckDuckGoSearchProvider(
        Settings(
            enable_browser_search=True,
            browser_search_max_queries=3,
            discovery_primary_topic_query_share=0.67,
            discovery_secondary_topics="Family Guy,Futurama",
        ),
        sample_offset=0,
    )
    plan = SearchPlan(
        query_families=[
            SearchQueryFamily(
                purpose="primary",
                queries=[
                    'The Simpsons "Homer Simpson" bowling screencap',
                    'The Simpsons "Lisa Simpson" library screenshot',
                    'The Simpsons "Bart Simpson" skateboard frame',
                    'The Simpsons "Marge Simpson" supermarket frame',
                ],
            ),
            SearchQueryFamily(
                purpose="secondary",
                queries=[
                    'Family Guy "Peter Griffin" kitchen screencap',
                    'Futurama "Bender" factory action screenshot',
                ],
            ),
        ],
        desired_visual_traits=[],
        excluded_concepts=[],
        desired_entities=[],
        desired_topics=["The Simpsons", "Family Guy", "Futurama"],
        desired_actions=[],
        desired_scenes=[],
        desired_compositions=[],
        source_policy="public_web_nsfw_blocked",
        rights_policy="provenance_only",
    )

    selected = provider._balanced_queries(plan)

    assert len(selected) == 3
    assert sum("The Simpsons" in query for query in selected) == 2
    assert sum(topic in query for topic in ("Family Guy", "Futurama") for query in selected) == 1
    compact = provider._request_query(
        'The Simpsons "Homer Simpson" repairing a broken appliance in the garage screencap'
    )
    assert compact == "Simpsons Homer Simpson repairing broken appliance garage screencap"
    assert len(compact.split()) == 8
