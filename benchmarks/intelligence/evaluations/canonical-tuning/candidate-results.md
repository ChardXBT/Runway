# RunWay Canonical Intelligence Evaluation

- **Artifact Version:** canonical-candidate-evaluation-v1

- **Dataset Version:** canonical-v1

- **Purpose:** tuning

## Splits

```json
[
  "tuning"
]
```

- **Configuration Hash:** 94a1f2c578b3ca14104f7142de71706559d78b3b196306da667c82bf5ab0be36

- **Started At:** 2026-07-18T23:57:38.253926+00:00

- **Completed At:** 2026-07-18T23:57:48.516402+00:00

- **Offline:** True

- **Paid Provider Calls:** 0

- **Publishing Mutations:** 0

## Dataset Integrity

```json
{
  "case_count": 10,
  "cluster_count": 10,
  "cross_split_clusters": 0,
  "dataset_version": "canonical-v1",
  "duplicate_case_ids": 0,
  "passed": true
}
```

## Pipeline

```json
{
  "analysis": {
    "batches": 2,
    "completed": 9,
    "deferred": 0,
    "edges": 36,
    "failed": 0,
    "skipped": 0
  },
  "capture": {
    "cursor": 13,
    "diagnostics": 1,
    "media_downloaded": 10,
    "posts_created": 12,
    "posts_seen": 12,
    "posts_updated": 1,
    "run_id": 1,
    "status": "completed"
  },
  "discovery": {
    "accepted": 23,
    "candidates": 30,
    "errors": [],
    "hard_rejected": 7,
    "provider": "fixture",
    "query_plan": {
      "desired_actions": [
        "clear visible action",
        "expressive reaction"
      ],
      "desired_compositions": [
        "close-up",
        "group scene",
        "wide composition"
      ],
      "desired_entities": [],
      "desired_scenes": [],
      "desired_topics": [
        "Synthetic Adventure"
      ],
      "desired_visual_traits": [
        "clear subject",
        "strong expression",
        "minimal overlay text"
      ],
      "excluded_concepts": [
        "fan art",
        "personal artwork",
        "artist portfolios",
        "commissions",
        "independent illustrations"
      ],
      "query_families": [
        {
          "purpose": "reaction frames",
          "queries": [
            "Synthetic Adventure expressive reaction scene",
            "Synthetic Adventure dramatic close up"
          ]
        },
        {
          "purpose": "discussion prompts",
          "queries": [
            "Synthetic Adventure character comparison",
            "Synthetic Adventure team decision scene"
          ]
        },
        {
          "purpose": "visual variety",
          "queries": [
            "Synthetic Adventure wide composition",
            "Synthetic Adventure colorful cinematic still"
          ]
        }
      ],
      "rights_policy": "unknown_requires_review",
      "source_policy": "preserve_and_review"
    },
    "run_id": 1,
    "status": "completed"
  },
  "profile_version": 1
}
```

## Metrics

```json
{
  "abstention_rate": 0.0,
  "case_count": 3,
  "grounding_pass_rate": 1.0,
  "mean_retrieval_role_coverage": 2.0,
  "unique_displayed_caption_rate": 0.555556,
  "unique_recommendation_rate": 0.666667,
  "unsupported_claim_rate": 0.0
}
```

## Cases

```json
[
  {
    "abstained": false,
    "abstention_reason": null,
    "candidate_analysis": {
      "caption_potential": 0.9,
      "characters": [
        "Fixture candidate 3"
      ],
      "composition": "centered",
      "confidence": 0.9,
      "emotion": "determination",
      "fan_art_probability": 0.0,
      "franchise": "Synthetic Ensemble",
      "personal_artwork_probability": 0.0,
      "scene_archetype": "reaction",
      "text_overlay": false,
      "unsafe_probability": 0.0,
      "watermark_probability": 0.02
    },
    "case_id": "qlob-fixture-candidate-09",
    "configuration_hash": "f18661fde46da145d7038eb44616dde287d22970aa814dee842ee8beda85d576",
    "displayed_captions": [
      "Why is Fixture so determined?",
      "Fixture's determined reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "duplicate": {
      "hard_rejection_reason": null,
      "novelty_score": 0.201025,
      "warnings": []
    },
    "fixture_uri": "fixture://candidate-09",
    "generated_pool": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9277890622615814,
          "pairing": 0.8848797500000001,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047141787166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.97775,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8848797500000001
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047141787166214,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so determined?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9797187764197588,
          "pairing": 0.8738798333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8786301420320995,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970933,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8738798333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8786301420320995,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's determined reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8721909666666667,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8815578372189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.954081,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8721909666666667
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8815578372189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8610267639160156,
          "pairing": 0.8611909333333334,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8535284080638335,
          "rotation": 0.9625096395611763,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.945443,
        "generation_index": 5,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8611909333333334
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8535284080638335,
          "trained": false
        },
        "structure": "observation",
        "text": "Every detail points back to Fixture.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8248448967933655,
          "pairing": 0.8738798333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8569477988844045,
          "rotation": 0.8248448967933655,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.943365,
        "generation_index": 7,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8738798333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8569477988844045,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture has entered the chat.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 0.8337529180751806,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8593602329492569,
          "pairing": 0.8424066571087712,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8696982588972468,
          "rotation": 0.8593602329492569,
          "structure_fit": 1.0,
          "style": 0.630763
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.936164,
        "generation_index": 3,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8424066571087712
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8696982588972468,
          "trained": false
        },
        "structure": "open_question",
        "text": "How would you explain Fixture's determined reaction?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "length_outside_channel_range:7/5-6",
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.794673353433609,
          "pairing": 0.8611909333333334,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8432620175464796,
          "rotation": 0.876617968082428,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.932264,
        "generation_index": 6,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8611909333333334
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8432620175464796,
          "trained": false
        },
        "structure": "observation",
        "text": "That determined look needs no explanation.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      }
    ],
    "grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "latency_ms": 146.682,
    "model_usage": {},
    "profile_version": 1,
    "prompt_version": "captions-v4",
    "recommendation": "Why is Fixture so determined?",
    "retrieval": {
      "channels": [
        "composition",
        "fusion",
        "lexical",
        "recent",
        "semantic",
        "topic",
        "visual"
      ],
      "considered_count": 34,
      "retrieval_run_id": 1,
      "role_coverage": [
        "caption_structure_example",
        "visual_analogue"
      ],
      "selected_evidence": [
        {
          "entity_id": 2,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09402592811924732,
          "retrieval_channel": "fusion",
          "selected_rank": 1
        },
        {
          "entity_id": 12,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.10205241786637136,
          "retrieval_channel": "fusion",
          "selected_rank": 2
        },
        {
          "entity_id": 7,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09878048780487805,
          "retrieval_channel": "fusion",
          "selected_rank": 3
        },
        {
          "entity_id": 6,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.10016538663410349,
          "retrieval_channel": "fusion",
          "selected_rank": 4
        },
        {
          "entity_id": 11,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09569672906031385,
          "retrieval_channel": "fusion",
          "selected_rank": 5
        },
        {
          "entity_id": 5,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09426120009364972,
          "retrieval_channel": "fusion",
          "selected_rank": 6
        },
        {
          "entity_id": 10,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07234042553191489,
          "retrieval_channel": "fusion",
          "selected_rank": 7
        }
      ]
    },
    "selected_candidates": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9277890622615814,
          "pairing": 0.8848797500000001,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047141787166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.97775,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8848797500000001
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047141787166214,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so determined?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9797187764197588,
          "pairing": 0.8738798333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8786301420320995,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970933,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8738798333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8786301420320995,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's determined reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:confident"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8721909666666667,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8815578372189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.954081,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8721909666666667
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8815578372189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:determined,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "determined",
          "reacting"
        ]
      }
    ],
    "split": "tuning"
  },
  {
    "abstained": false,
    "abstention_reason": null,
    "candidate_analysis": {
      "caption_potential": 0.9,
      "characters": [
        "Fixture candidate 6"
      ],
      "composition": "centered",
      "confidence": 0.9,
      "emotion": "surprise",
      "fan_art_probability": 0.0,
      "franchise": "Synthetic Adventure",
      "personal_artwork_probability": 0.0,
      "scene_archetype": "reaction",
      "text_overlay": false,
      "unsafe_probability": 0.0,
      "watermark_probability": 0.02
    },
    "case_id": "qlob-fixture-candidate-19",
    "configuration_hash": "f18661fde46da145d7038eb44616dde287d22970aa814dee842ee8beda85d576",
    "displayed_captions": [
      "Why is Fixture so surprised?",
      "Fixture's surprised reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "duplicate": {
      "hard_rejection_reason": null,
      "novelty_score": 0.099967,
      "warnings": [
        "historical date precision cannot prove the 180-day boundary"
      ]
    },
    "fixture_uri": "fixture://candidate-19",
    "generated_pool": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9293289035558701,
          "pairing": 0.8805649499999999,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047571644978218,
          "rotation": 0.9293289035558701,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977602,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8805649499999999
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047571644978218,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so surprised?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9617900066077709,
          "pairing": 0.8695650333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8759475222584212,
          "rotation": 0.9617900066077709,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.967319,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8695650333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8759475222584212,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's surprised reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8678761666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8813852452189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953658,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8678761666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8813852452189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8610267639160156,
          "pairing": 0.8568761333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8533558160638335,
          "rotation": 0.9625096395611763,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.94502,
        "generation_index": 5,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8568761333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8533558160638335,
          "trained": false
        },
        "structure": "observation",
        "text": "Every detail points back to Fixture.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8248448967933655,
          "pairing": 0.8695650333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8567752068844046,
          "rotation": 0.8248448967933655,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.942943,
        "generation_index": 7,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8695650333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8567752068844046,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture has entered the chat.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8042182922363281,
          "pairing": 0.8568761333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8480237450277333,
          "rotation": 0.958123467862606,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.938577,
        "generation_index": 6,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8568761333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8480237450277333,
          "trained": false
        },
        "structure": "observation",
        "text": "That surprised look needs no explanation.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 0.8337529180751806,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8638239651918411,
          "pairing": 0.838091857108771,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8701505894112086,
          "rotation": 0.8638239651918411,
          "structure_fit": 1.0,
          "style": 0.630763
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.936536,
        "generation_index": 3,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.838091857108771
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8701505894112086,
          "trained": false
        },
        "structure": "open_question",
        "text": "How would you explain Fixture's surprised reaction?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "length_outside_channel_range:7/5-6",
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      }
    ],
    "grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "latency_ms": 140.701,
    "model_usage": {},
    "profile_version": 1,
    "prompt_version": "captions-v4",
    "recommendation": "Why is Fixture so surprised?",
    "retrieval": {
      "channels": [
        "composition",
        "emotion",
        "fusion",
        "lexical",
        "recent",
        "semantic",
        "topic",
        "visual"
      ],
      "considered_count": 37,
      "retrieval_run_id": 2,
      "role_coverage": [
        "caption_structure_example",
        "visual_analogue"
      ],
      "selected_evidence": [
        {
          "entity_id": 7,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.14390243902439023,
          "retrieval_channel": "fusion",
          "selected_rank": 1
        },
        {
          "entity_id": 5,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.11871551590780179,
          "retrieval_channel": "fusion",
          "selected_rank": 2
        },
        {
          "entity_id": 12,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07421934422859491,
          "retrieval_channel": "fusion",
          "selected_rank": 3
        },
        {
          "entity_id": 10,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09929481146872451,
          "retrieval_channel": "fusion",
          "selected_rank": 4
        },
        {
          "entity_id": 11,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.11831023958080111,
          "retrieval_channel": "fusion",
          "selected_rank": 5
        },
        {
          "entity_id": 2,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09447979511507588,
          "retrieval_channel": "fusion",
          "selected_rank": 6
        },
        {
          "entity_id": 6,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07271045328399629,
          "retrieval_channel": "fusion",
          "selected_rank": 7
        }
      ]
    },
    "selected_candidates": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9293289035558701,
          "pairing": 0.8805649499999999,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047571644978218,
          "rotation": 0.9293289035558701,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977602,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8805649499999999
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047571644978218,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so surprised?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9617900066077709,
          "pairing": 0.8695650333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8759475222584212,
          "rotation": 0.9617900066077709,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.967319,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8695650333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8759475222584212,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's surprised reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8678761666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8813852452189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953658,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8678761666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8813852452189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      }
    ],
    "split": "tuning"
  },
  {
    "abstained": false,
    "abstention_reason": null,
    "candidate_analysis": {
      "caption_potential": 0.8400000000000001,
      "characters": [
        "Fixture candidate 4"
      ],
      "composition": "centered",
      "confidence": 0.9,
      "emotion": "surprise",
      "fan_art_probability": 0.0,
      "franchise": "Synthetic Comedy",
      "personal_artwork_probability": 0.0,
      "scene_archetype": "reaction",
      "text_overlay": false,
      "unsafe_probability": 0.0,
      "watermark_probability": 0.02
    },
    "case_id": "qlob-fixture-candidate-17",
    "configuration_hash": "f18661fde46da145d7038eb44616dde287d22970aa814dee842ee8beda85d576",
    "displayed_captions": [
      "Why is Fixture so surprised?",
      "Fixture's surprised reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "duplicate": {
      "hard_rejection_reason": null,
      "novelty_score": 0.190017,
      "warnings": []
    },
    "fixture_uri": "fixture://candidate-17",
    "generated_pool": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9293289035558701,
          "pairing": 0.88061185,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047590404978219,
          "rotation": 0.9293289035558701,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977606,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.88061185
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047590404978219,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so surprised?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9617900066077709,
          "pairing": 0.8696119333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8759493982584212,
          "rotation": 0.9617900066077709,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.967323,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8696119333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8759493982584212,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's surprised reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8679230666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8813871212189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953663,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8679230666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8813871212189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8610267639160156,
          "pairing": 0.8569230333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8533576920638335,
          "rotation": 0.9625096395611763,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.945024,
        "generation_index": 5,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8569230333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8533576920638335,
          "trained": false
        },
        "structure": "observation",
        "text": "Every detail points back to Fixture.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8248448967933655,
          "pairing": 0.8696119333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8567770828844046,
          "rotation": 0.8248448967933655,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.942947,
        "generation_index": 7,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8696119333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8567770828844046,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture has entered the chat.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8042182922363281,
          "pairing": 0.8569230333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8480256210277333,
          "rotation": 0.958123467862606,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.938582,
        "generation_index": 6,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8569230333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8480256210277333,
          "trained": false
        },
        "structure": "observation",
        "text": "That surprised look needs no explanation.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 0.8337529180751806,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8638239651918411,
          "pairing": 0.8381387571087711,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8701524654112086,
          "rotation": 0.8638239651918411,
          "structure_fit": 1.0,
          "style": 0.630763
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.93654,
        "generation_index": 3,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8381387571087711
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8701524654112086,
          "trained": false
        },
        "structure": "open_question",
        "text": "How would you explain Fixture's surprised reaction?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "length_outside_channel_range:7/5-6",
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      }
    ],
    "grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "latency_ms": 177.48,
    "model_usage": {},
    "profile_version": 1,
    "prompt_version": "captions-v4",
    "recommendation": "Why is Fixture so surprised?",
    "retrieval": {
      "channels": [
        "composition",
        "emotion",
        "fusion",
        "lexical",
        "recent",
        "semantic",
        "topic",
        "visual"
      ],
      "considered_count": 38,
      "retrieval_run_id": 3,
      "role_coverage": [
        "caption_structure_example",
        "visual_analogue"
      ],
      "selected_evidence": [
        {
          "entity_id": 5,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.14296800760215395,
          "retrieval_channel": "fusion",
          "selected_rank": 1
        },
        {
          "entity_id": 11,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.1418820733057829,
          "retrieval_channel": "fusion",
          "selected_rank": 2
        },
        {
          "entity_id": 2,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.11947609962357551,
          "retrieval_channel": "fusion",
          "selected_rank": 3
        },
        {
          "entity_id": 6,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.0744208037825059,
          "retrieval_channel": "fusion",
          "selected_rank": 4
        },
        {
          "entity_id": 7,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.11875320746562948,
          "retrieval_channel": "fusion",
          "selected_rank": 5
        },
        {
          "entity_id": 10,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07384520505704595,
          "retrieval_channel": "fusion",
          "selected_rank": 6
        },
        {
          "entity_id": 12,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07354301572617947,
          "retrieval_channel": "fusion",
          "selected_rank": 7
        }
      ]
    },
    "selected_candidates": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9293289035558701,
          "pairing": 0.88061185,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9047590404978219,
          "rotation": 0.9293289035558701,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977606,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.88061185
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9047590404978219,
          "trained": false
        },
        "structure": "open_question",
        "text": "Why is Fixture so surprised?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9617900066077709,
          "pairing": 0.8696119333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8759493982584212,
          "rotation": 0.9617900066077709,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.967323,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8696119333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8759493982584212,
          "trained": false
        },
        "structure": "observation",
        "text": "Fixture's surprised reaction says plenty.",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture",
            "visible_emotion:surprised"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      },
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.8592357039451599,
          "pairing": 0.8679230666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8813871212189891,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953663,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8679230666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8813871212189891,
          "trained": false
        },
        "structure": "open_question",
        "text": "What has Fixture reacting like this?",
        "uncertainty": [],
        "verification": {
          "checks": {
            "entities": true,
            "events": true,
            "language": true,
            "policy": true,
            "quotes": true,
            "relationships": true
          },
          "grounding_score": 1.0,
          "passed": true,
          "policy_score": 1.0,
          "supported_claims": [
            "visible_entity:Fixture"
          ],
          "unsupported_claims": [],
          "warnings": [
            "unverified_candidate_evidence:surprised,reacting"
          ]
        },
        "visible_evidence": [
          "Fixture",
          "surprised",
          "reacting"
        ]
      }
    ],
    "split": "tuning"
  }
]
```
