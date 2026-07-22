# Runway Canonical Intelligence Evaluation

- **Artifact Version:** canonical-candidate-evaluation-v1

- **Dataset Version:** canonical-v1

- **Purpose:** holdout_release

## Splits

```json
[
  "locked_holdout"
]
```

- **Configuration Hash:** 94a1f2c578b3ca14104f7142de71706559d78b3b196306da667c82bf5ab0be36

- **Started At:** 2026-07-19T00:05:47.892707+00:00

- **Completed At:** 2026-07-19T00:05:57.916756+00:00

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
  "case_count": 2,
  "grounding_pass_rate": 1.0,
  "mean_retrieval_role_coverage": 2.0,
  "unique_displayed_caption_rate": 0.5,
  "unique_recommendation_rate": 0.5,
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
      "caption_potential": 0.87,
      "characters": [
        "Fixture candidate 4"
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
    "case_id": "qlob-fixture-candidate-03",
    "configuration_hash": "f18661fde46da145d7038eb44616dde287d22970aa814dee842ee8beda85d576",
    "displayed_captions": [
      "Why is Fixture so determined?",
      "Fixture's determined reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "duplicate": {
      "hard_rejection_reason": null,
      "novelty_score": 0.111092,
      "warnings": [
        "historical date precision cannot prove the 180-day boundary"
      ]
    },
    "fixture_uri": "fixture://candidate-03",
    "generated_pool": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9277890622615814,
          "pairing": 0.8793263,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9044920407166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977206,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8793263
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9044920407166214,
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
          "pairing": 0.8683263833333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8784080040320996,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970389,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8683263833333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8784080040320996,
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
          "pairing": 0.8666375166666667,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.881335699218989,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953537,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8666375166666667
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.881335699218989,
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
          "pairing": 0.8556374833333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8533062700638334,
          "rotation": 0.9625096395611763,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.944898,
        "generation_index": 5,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8556374833333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8533062700638334,
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
          "pairing": 0.8683263833333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8567256608844046,
          "rotation": 0.8248448967933655,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.942821,
        "generation_index": 7,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8683263833333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8567256608844046,
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
          "pairing": 0.8368532071087711,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8694761208972468,
          "rotation": 0.8593602329492569,
          "structure_fit": 1.0,
          "style": 0.630763
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.93562,
        "generation_index": 3,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8368532071087711
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8694761208972468,
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
          "pairing": 0.8556374833333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8430398795464795,
          "rotation": 0.876617968082428,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.93172,
        "generation_index": 6,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8556374833333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8430398795464795,
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
    "latency_ms": 145.97,
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
          "fusion_score": 0.09390008497014835,
          "retrieval_channel": "fusion",
          "selected_rank": 1
        },
        {
          "entity_id": 12,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09968567664219838,
          "retrieval_channel": "fusion",
          "selected_rank": 2
        },
        {
          "entity_id": 7,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09776760392188638,
          "retrieval_channel": "fusion",
          "selected_rank": 3
        },
        {
          "entity_id": 6,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09922798299549727,
          "retrieval_channel": "fusion",
          "selected_rank": 4
        },
        {
          "entity_id": 11,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09839289090565323,
          "retrieval_channel": "fusion",
          "selected_rank": 5
        },
        {
          "entity_id": 5,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.0953603615770377,
          "retrieval_channel": "fusion",
          "selected_rank": 6
        },
        {
          "entity_id": 10,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.07298797409805735,
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
          "pairing": 0.8793263,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9044920407166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977206,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8793263
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9044920407166214,
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
          "pairing": 0.8683263833333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8784080040320996,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970389,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8683263833333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8784080040320996,
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
          "pairing": 0.8666375166666667,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.881335699218989,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953537,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8666375166666667
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.881335699218989,
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
    "split": "locked_holdout"
  },
  {
    "abstained": false,
    "abstention_reason": null,
    "candidate_analysis": {
      "caption_potential": 0.8400000000000001,
      "characters": [
        "Fixture candidate 7"
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
    "case_id": "qlob-fixture-candidate-27",
    "configuration_hash": "f18661fde46da145d7038eb44616dde287d22970aa814dee842ee8beda85d576",
    "displayed_captions": [
      "Why is Fixture so determined?",
      "Fixture's determined reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "duplicate": {
      "hard_rejection_reason": null,
      "novelty_score": 0.116858,
      "warnings": []
    },
    "fixture_uri": "fixture://candidate-27",
    "generated_pool": [
      {
        "attempt_number": 1,
        "components": {
          "grounding": 1.0,
          "length_fit": 1.0,
          "negative_feedback_risk": 0.0,
          "novelty": 0.9277890622615814,
          "pairing": 0.8790494499999999,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9044809667166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977179,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8790494499999999
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9044809667166214,
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
          "pairing": 0.8680495333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8783969300320996,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970362,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8680495333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8783969300320996,
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
          "pairing": 0.8663606666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.881324625218989,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953509,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8663606666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.881324625218989,
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
          "pairing": 0.8553606333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8532951960638334,
          "rotation": 0.9625096395611763,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.944871,
        "generation_index": 5,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8553606333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8532951960638334,
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
          "pairing": 0.8680495333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8567145868844046,
          "rotation": 0.8248448967933655,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.942794,
        "generation_index": 7,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8680495333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8567145868844046,
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
          "pairing": 0.836576357108771,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8694650468972468,
          "rotation": 0.8593602329492569,
          "structure_fit": 1.0,
          "style": 0.630763
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.935592,
        "generation_index": 3,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.836576357108771
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8694650468972468,
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
          "pairing": 0.8553606333333332,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8430288055464795,
          "rotation": 0.876617968082428,
          "structure_fit": 0.8200000000000001,
          "style": 0.805524
        },
        "editorial_angle": "reaction",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.931693,
        "generation_index": 6,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8553606333333332
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8430288055464795,
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
    "latency_ms": 142.171,
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
      "retrieval_run_id": 2,
      "role_coverage": [
        "caption_structure_example",
        "visual_analogue"
      ],
      "selected_evidence": [
        {
          "entity_id": 2,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09467527876859796,
          "retrieval_channel": "fusion",
          "selected_rank": 1
        },
        {
          "entity_id": 12,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09897860593512767,
          "retrieval_channel": "fusion",
          "selected_rank": 2
        },
        {
          "entity_id": 7,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09776760392188638,
          "retrieval_channel": "fusion",
          "selected_rank": 3
        },
        {
          "entity_id": 6,
          "entity_type": "post",
          "evidence_role": "caption_structure_example",
          "fusion_score": 0.09987553156163972,
          "retrieval_channel": "fusion",
          "selected_rank": 4
        },
        {
          "entity_id": 11,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.09761769710720362,
          "retrieval_channel": "fusion",
          "selected_rank": 5
        },
        {
          "entity_id": 5,
          "entity_type": "post",
          "evidence_role": "visual_analogue",
          "fusion_score": 0.0960674322841084,
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
          "pairing": 0.8790494499999999,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.9044809667166214,
          "rotation": 0.9277890622615814,
          "structure_fit": 1.0,
          "style": 0.828571
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.977179,
        "generation_index": 1,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8790494499999999
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.9044809667166214,
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
          "pairing": 0.8680495333333333,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.8783969300320996,
          "rotation": 0.9797187764197588,
          "structure_fit": 0.8200000000000001,
          "style": 0.914286
        },
        "editorial_angle": "observation",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.970362,
        "generation_index": 4,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8680495333333333
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.8783969300320996,
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
          "pairing": 0.8663606666666666,
          "policy": 1.0,
          "positive_feedback": 0.0,
          "preference": 0.881324625218989,
          "rotation": 0.8592357039451599,
          "structure_fit": 1.0,
          "style": 0.71981
        },
        "editorial_angle": "audience_inquiry",
        "eligible": true,
        "exclusion_reasons": [],
        "final_score": 0.953509,
        "generation_index": 2,
        "generator_confidence": 0.84,
        "generic_penalty": 0.0,
        "pairing": {
          "reason": "deterministic image-caption compatibility baseline",
          "score": 0.8663606666666666
        },
        "preference": {
          "calibrated": false,
          "reason": "deterministic fallback: 0 pairwise labels",
          "sample_count": 0,
          "score": 0.881324625218989,
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
    "split": "locked_holdout"
  }
]
```
