from __future__ import annotations

from runway.captions.service import _accumulate_usage


def test_caption_pipeline_usage_sums_every_model_call() -> None:
    usage = _accumulate_usage(
        {},
        {
            "input_tokens": 10,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 4},
        },
    )
    usage = _accumulate_usage(
        usage,
        {
            "input_tokens": 7,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 2},
        },
    )

    assert usage == {
        "input_tokens": 17,
        "output_tokens": 8,
        "input_tokens_details": {"cached_tokens": 6},
        "model_calls": 2,
    }
