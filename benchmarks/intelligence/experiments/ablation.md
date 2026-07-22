# Runway Intelligence Ablation

- **Artifact Version:** canonical-ablation-v1

- **Dataset Version:** canonical-v1

- **Seed:** 20260718

## Full Engine

```json
{
  "abstention_rate": 0.0,
  "case_count": 3,
  "cases": [
    {
      "abstained": false,
      "accepted_proxy": true,
      "case_id": "qlob-fixture-candidate-09",
      "expected_term_hit": true,
      "forbidden_term_hit": false,
      "grounded": true,
      "grounding_score": 1.0,
      "pairwise_correct_proxy": true,
      "selected_structure": "open_question",
      "selected_text": "Why is Fixture so determined?",
      "unsupported_claims": 0
    },
    {
      "abstained": false,
      "accepted_proxy": true,
      "case_id": "qlob-fixture-candidate-19",
      "expected_term_hit": true,
      "forbidden_term_hit": false,
      "grounded": true,
      "grounding_score": 1.0,
      "pairwise_correct_proxy": true,
      "selected_structure": "open_question",
      "selected_text": "Why is Fixture so surprised?",
      "unsupported_claims": 0
    },
    {
      "abstained": false,
      "accepted_proxy": true,
      "case_id": "qlob-fixture-candidate-17",
      "expected_term_hit": true,
      "forbidden_term_hit": false,
      "grounded": true,
      "grounding_score": 1.0,
      "pairwise_correct_proxy": true,
      "selected_structure": "open_question",
      "selected_text": "Why is Fixture so surprised?",
      "unsupported_claims": 0
    }
  ],
  "grounding_pass_rate": 1.0,
  "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
  "no_edit_acceptance_proxy": 1.0,
  "objective": 1.0,
  "pairwise_preference_accuracy_proxy": 1.0,
  "unsupported_claim_rate": 0.0
}
```

## Ablations

```json
[
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "grounding"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "preference"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "style"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "novelty"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "rotation"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "positive_feedback"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "negative_feedback_risk"
  },
  {
    "metrics": {
      "abstention_rate": 0.0,
      "case_count": 3,
      "cases": [
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-09",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so determined?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-19",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        },
        {
          "abstained": false,
          "accepted_proxy": true,
          "case_id": "qlob-fixture-candidate-17",
          "expected_term_hit": true,
          "forbidden_term_hit": false,
          "grounded": true,
          "grounding_score": 1.0,
          "pairwise_correct_proxy": true,
          "selected_structure": "open_question",
          "selected_text": "Why is Fixture so surprised?",
          "unsupported_claims": 0
        }
      ],
      "grounding_pass_rate": 1.0,
      "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
      "no_edit_acceptance_proxy": 1.0,
      "objective": 1.0,
      "pairwise_preference_accuracy_proxy": 1.0,
      "unsupported_claim_rate": 0.0
    },
    "objective_delta": 0.0,
    "removed_component": "pairing"
  }
]
```

- **Model Calls:** 0
