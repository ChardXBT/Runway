from __future__ import annotations

import pytest

from runway.evaluation.generalization import evaluate_generalization_fixtures


@pytest.mark.asyncio
async def test_same_image_adapts_to_five_channel_histories_without_leakage() -> None:
    result = await evaluate_generalization_fixtures()
    assert result["channel_count"] == 5
    assert result["unique_caption_count"] == 5
    assert result["same_image_different_captions"] is True
    assert result["channel_policy_isolation"] is True
    assert result["all_expected_structures"] is True
    assert result["all_grounded"] is True
    assert result["passed"] is True
