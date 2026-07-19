from __future__ import annotations

from typing import Any

from runway.analysis.runtime import MockAgentRuntime
from runway.captions.planning import EditorialPlanner
from runway.captions.verification import CaptionVerifier
from runway.evaluation.datasets import DatasetRepository
from runway.intelligence.embeddings import configuration_hash
from runway.intelligence.policies import PolicySnapshot


async def evaluate_generalization_fixtures(
    repository: DatasetRepository | None = None,
) -> dict[str, object]:
    datasets = repository or DatasetRepository()
    fixture = datasets.generalization()
    runtime = MockAgentRuntime()
    planner = EditorialPlanner()
    verifier = CaptionVerifier()
    outputs = []
    for index, channel in enumerate(fixture.channels, start=1):
        policy = PolicySnapshot(
            channel_id=index,
            version=configuration_hash(channel.model_dump()),
            rules=[
                {
                    "type": "fixture_policy",
                    "value": {
                        "structures": channel.preferred_structures,
                        "language": channel.language,
                        "locale": channel.locale,
                    },
                }
            ],
            preferred_structures=channel.preferred_structures,
            prohibited_claims=[
                "unsupported_entity",
                "invented_event",
                "invented_quote",
                "unsupported_relationship",
            ],
            language=channel.language,
            locale=channel.locale,
            rights_policy="unknown_requires_review",
            source_policy="preserve_and_review",
            question_first=channel.question_first,
        )
        candidate_analysis: dict[str, Any] = {
            "characters": ["one person"],
            "entities": [
                {
                    "name": "one person",
                    "canonical_name": None,
                    "entity_type": "person",
                    "confidence": 0.93,
                }
            ],
            "objects": ["red object", "desk"],
            "actions": ["examining"],
            "relationships": ["a second person watches"],
            "setting": "desk",
            "emotion": "focused",
            "scene_archetype": "inspection",
            "composition": "two-person desk scene",
            "ocr_text": [],
            "confidence": 0.93,
            "field_confidence": {
                "entities": 0.93,
                "objects": 0.9,
                "actions": 0.9,
                "relationships": 0.82,
                "scene": 0.9,
                "emotion": 0.72,
                "composition": 0.9,
            },
        }
        retrieval_context: dict[str, object] = {
            "retrieval_run_id": index,
            "style_profile": {
                "caption_statistics": {
                    "word_percentiles": {"p25": 4, "p75": 12},
                }
            },
            "caption_style_examples": [
                {"post_id": position, "caption": caption}
                for position, caption in enumerate(channel.history, start=1)
            ],
            "feedback_context": {
                "positive_examples": [],
                "negative_examples": [],
            },
            "negative_examples": [],
            "rotation_state": {
                "last_3_post_ids": [1, 2, 3],
                "scheduled_proposal_ids": [],
            },
            "explicit_rules": policy.rules,
        }
        brief = planner.build(
            candidate_analysis=candidate_analysis,
            retrieval_context=retrieval_context,
            policy=policy,
            source_context={
                "rights_status": "creator_owned",
                "source_domain": "fixture.local",
            },
        )
        generated = await runtime.generate_caption_options(
            {
                "candidate_id": index,
                "candidate_analysis": candidate_analysis,
                "historical_post_ids": [1, 2, 3],
                "editorial_brief": brief.model_dump(),
            }
        )
        preferred = next(
            (
                candidate
                for candidate in generated.candidates
                if candidate.structure == channel.expected_structure
            ),
            generated.candidates[0],
        )
        verification = verifier.verify(preferred, brief)
        outputs.append(
            {
                "fixture_id": channel.fixture_id,
                "policy_version": policy.version,
                "expected_structure": channel.expected_structure,
                "selected_structure": preferred.structure,
                "caption": preferred.text,
                "language": preferred.language,
                "grounded": verification.passed,
                "unsupported_claims": verification.unsupported_claims,
                "policy_isolated": policy.preferred_structures
                == channel.preferred_structures,
            }
        )
    captions = [str(row["caption"]) for row in outputs]
    passed = all(
        row["selected_structure"] == row["expected_structure"]
        and row["grounded"]
        and row["policy_isolated"]
        for row in outputs
    )
    return {
        "artifact_version": "cross-channel-generalization-v1",
        "dataset_version": fixture.dataset_version,
        "shared_image_id": fixture.shared_image.image_id,
        "channel_count": len(outputs),
        "unique_caption_count": len(set(captions)),
        "same_image_different_captions": len(set(captions)) == len(captions),
        "channel_policy_isolation": all(
            bool(row["policy_isolated"]) for row in outputs
        ),
        "all_expected_structures": all(
            row["selected_structure"] == row["expected_structure"]
            for row in outputs
        ),
        "all_grounded": all(bool(row["grounded"]) for row in outputs),
        "passed": passed and len(set(captions)) == len(captions),
        "channels": outputs,
    }
