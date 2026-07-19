from __future__ import annotations

import pytest

from runway.analysis.runtime import MockAgentRuntime


@pytest.mark.asyncio
async def test_historical_visual_fields_do_not_change_with_caption_wording() -> None:
    runtime = MockAgentRuntime()
    first = await runtime.annotate_historical_post(
        {
            "post_id": 14,
            "caption": "A sports team celebrates the championship?",
        }
    )
    second = await runtime.annotate_historical_post(
        {
            "post_id": 14,
            "caption": "A quiet laboratory observation.",
        }
    )

    assert first.entities == second.entities
    assert first.visible_characters == second.visible_characters
    assert first.objects == second.objects
    assert first.actions == second.actions
    assert first.relationships == second.relationships
    assert first.setting == second.setting
    assert first.composition == second.composition
    assert first.facial_emotional_cues == second.facial_emotional_cues
    assert first.caption_intent != second.caption_intent
