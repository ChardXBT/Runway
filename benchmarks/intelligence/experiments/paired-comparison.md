# RunWay Paired Baseline Comparison

- **Artifact Version:** paired-baseline-comparison-v1

- **Baseline Id:** baseline-876fe5f

- **Candidate Configuration Hash:** 94a1f2c578b3ca14104f7142de71706559d78b3b196306da667c82bf5ab0be36

- **Identical Case Ids:** True

- **Case Count:** 3

## Baseline Metrics

```json
{
  "abstention_rate": 0.0,
  "case_count": 8,
  "fixture_pipeline": {
    "analysis": {
      "batches": 2,
      "completed": 9,
      "deferred": 0,
      "edges": 36,
      "failed": 0,
      "skipped": 0
    },
    "capture_status": "completed",
    "discovery": {
      "accepted": 23,
      "candidates": 30,
      "hard_rejected": 7
    }
  },
  "open_question_recommendation_rate": 1.0,
  "profile_evaluation": {
    "duplicate_detection": {
      "documented_phash_threshold": 0.88,
      "documented_semantic_threshold": 0.99,
      "evaluation_source": "controlled synthetic transformations",
      "exact_identity_passed": true,
      "positive_variants": 4,
      "transformed_true_positive_rate": 1.0,
      "unrelated_false_positive_rate": 0.0
    },
    "holdout_fraction": 0.222222,
    "holdout_samples": 2,
    "image_caption_matching": {
      "accuracy": 1.0,
      "errors": [],
      "evaluated": 2
    },
    "limitations": [
      "Image-caption matching, caption ranking, and retrieval relevance use the captured channel holdout; duplicate transformation recall uses controlled synthetic fixtures.",
      "Reviewed annotation accuracy covers only fields with a recorded human review and uses an uncertainty/outlier sample, so it is not an unbiased full-catalogue accuracy estimate.",
      "YouTube exposed relative publication dates, so reconstructed timestamps are approximate.",
      "The system builds a retrieval profile; it does not fine-tune model weights."
    ],
    "profile_version": 1,
    "qlob_caption_ranking_accuracy": 0.5,
    "retrieval_top3_franchise_relevance": 0.5,
    "review_sample_strategy": "manual uncertainty/outlier audit; not a random sample",
    "reviewed_annotation_field_accuracy": null,
    "reviewed_annotation_fields": 0,
    "reviewed_annotation_posts": 0,
    "sample_errors": [],
    "training_samples": 7
  },
  "selected_grounding_flag_rate": 0.333333,
  "unique_displayed_caption_rate": 0.166667,
  "unique_recommendation_rate": 0.25
}
```

## Candidate Metrics

```json
{
  "abstention_rate": 0.0,
  "case_count": 3,
  "grounding_pass_rate": 1.0,
  "mean_retrieval_role_coverage": 1.666667,
  "unique_displayed_caption_rate": 0.555556,
  "unique_recommendation_rate": 0.666667,
  "unsupported_claim_rate": 0.0
}
```

## Paired Cases

```json
[
  {
    "case_id": "qlob-fixture-candidate-01",
    "evidence_overlap_jaccard": 1.0,
    "new_abstained": false,
    "new_displayed": [
      "Why is Fixture so surprised?",
      "Fixture's surprised reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "new_grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "new_latency_ms": 182.338,
    "new_recommendation": "Why is Fixture so surprised?",
    "new_role_coverage": [
      "caption_structure_example",
      "visual_analogue"
    ],
    "old_abstained": false,
    "old_displayed": [
      "Why is Fixture so surprised?",
      "That suspicious silence says everything.",
      "That escalated with impressive speed."
    ],
    "old_latency_ms": null,
    "old_recommendation": "Why is Fixture so surprised?"
  },
  {
    "case_id": "qlob-fixture-candidate-04",
    "evidence_overlap_jaccard": 0.714286,
    "new_abstained": false,
    "new_displayed": [
      "Why is Fixture so surprised?",
      "Fixture's surprised reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "new_grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "new_latency_ms": 124.501,
    "new_recommendation": "Why is Fixture so surprised?",
    "new_role_coverage": [
      "visual_analogue"
    ],
    "old_abstained": false,
    "old_displayed": [
      "Why is Fixture so surprised?",
      "That suspicious silence says everything.",
      "That escalated with impressive speed."
    ],
    "old_latency_ms": null,
    "old_recommendation": "Why is Fixture so surprised?"
  },
  {
    "case_id": "qlob-fixture-candidate-18",
    "evidence_overlap_jaccard": 1.0,
    "new_abstained": false,
    "new_displayed": [
      "Why is Fixture so determined?",
      "Fixture's determined reaction says plenty.",
      "What has Fixture reacting like this?"
    ],
    "new_grounding": {
      "passed": true,
      "unsupported_claims": []
    },
    "new_latency_ms": 141.972,
    "new_recommendation": "Why is Fixture so determined?",
    "new_role_coverage": [
      "caption_structure_example",
      "visual_analogue"
    ],
    "old_abstained": false,
    "old_displayed": [
      "Why is Fixture so confident?",
      "That suspicious silence says everything.",
      "That escalated with impressive speed."
    ],
    "old_latency_ms": null,
    "old_recommendation": "Why is Fixture so confident?"
  }
]
```
