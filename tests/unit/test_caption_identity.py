import pytest

from runway.analysis.runtime import MockAgentRuntime


@pytest.mark.asyncio
async def test_caption_generation_uses_grounded_natural_identity_reference() -> None:
    runtime = MockAgentRuntime()
    named = await runtime.generate_caption_options(
        {
            "candidate_id": 1,
            "candidate_analysis": {
                "characters": ["Homer Simpson"],
                "emotion": "excitement",
                "actions": ["reacting"],
            },
            "historical_post_ids": [],
            "editorial_brief": {
                "target_structures": ["open_question", "observation", "reaction"],
                "target_language": "en",
            },
        }
    )
    unidentified = await runtime.generate_caption_options(
        {
            "candidate_id": 2,
            "candidate_analysis": {
                "characters": ["unidentified character"],
                "emotion": "surprise",
                "actions": ["reacting"],
            },
            "historical_post_ids": [],
            "editorial_brief": {
                "target_structures": ["open_question", "observation", "reaction"],
                "target_language": "en",
            },
        }
    )

    assert named.candidates[0].text == "Why is Homer so excited?"
    assert unidentified.candidates[0].text == "Why is the subject so surprised?"


@pytest.mark.asyncio
async def test_mock_caption_never_exposes_unknown_as_an_emotion() -> None:
    runtime = MockAgentRuntime()
    result = await runtime.generate_caption_options(
        {
            "candidate_id": 9,
            "candidate_analysis": {
                "characters": ["unidentified character"],
                "emotion": "unknown",
                "actions": ["standing"],
            },
            "editorial_brief": {"target_structures": ["open_question"]},
        }
    )

    assert result.candidates[0].text == "Why is the subject so curious?"
    assert all("unknown" not in candidate.text.casefold() for candidate in result.candidates)
