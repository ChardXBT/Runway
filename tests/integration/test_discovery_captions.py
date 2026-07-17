import json

import pytest

from leeway.analysis.service import AnalysisService
from leeway.captions.service import CaptionService
from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.discovery.providers import BrowserSearchProvider, ManualUrlProvider
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService


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
    from leeway.analysis.schemas import SearchPlan

    provider = ManualUrlProvider(["file:///secret.jpg"])
    with pytest.raises(ValueError, match="HTTP"):
        await provider.search(
            SearchPlan(query_families=[], desired_visual_traits=[], excluded_concepts=[])
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


def test_bing_metadata_parser_preserves_direct_and_source_urls() -> None:
    parsed = BrowserSearchProvider._parse_bing_metadata(
        [
            json.dumps(
                {
                    "murl": "https://images.example.test/frame.jpg",
                    "purl": "https://source.example.test/article",
                    "ow": 1280,
                    "oh": 720,
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
        }
    ]
