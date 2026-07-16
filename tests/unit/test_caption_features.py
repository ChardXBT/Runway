import numpy as np
import pytest

from leeway.analysis.features import (
    aggregate_caption_features,
    caption_features,
    text_embedding,
)


def test_caption_features_are_deterministic_and_local() -> None:
    row = caption_features("Would you trust this plan?!")
    assert row["word_count"] == 5
    assert row["has_question"] is True
    assert row["second_person_count"] == 1
    first = text_embedding("Would you trust this plan?")
    second = text_embedding("Would you trust this plan?")
    assert np.array_equal(first, second)


def test_caption_aggregate_reports_real_frequencies() -> None:
    stats = aggregate_caption_features(["Really?", "Absolutely!", "No context."])
    assert stats["sample_size"] == 3
    assert stats["question_frequency"] == pytest.approx(1 / 3, abs=1e-6)
    assert stats["exclamation_frequency"] == pytest.approx(1 / 3, abs=1e-6)
