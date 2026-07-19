from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

HARD_GATES = (
    "zero_channel_data_leakage",
    "zero_migration_corruption",
    "zero_publishing_safety_regression",
    "zero_hidden_paid_provider_fallback",
    "zero_rights_policy_regression",
    "zero_duplicate_safety_regression",
    "zero_unsupported_critical_entity_claims",
    "required_deterministic_workflows",
    "required_rollback_procedures",
)

CRITICAL_REGRESSIONS = (
    "wrong_character_prevention",
    "invented_plot_prevention",
    "invented_quote_prevention",
    "duplicate_rejection",
    "rights_blocking",
    "channel_isolation",
    "explicit_rule_enforcement",
    "abstention_without_grounded_caption",
)


def replacement_gate_report(
    *,
    hard_gates: dict[str, bool],
    critical_regressions: dict[str, bool],
    primary_quality: dict[str, Any],
    secondary_no_material_regression: bool,
    cross_channel_generalization: bool,
    holdout_use_count: int,
    canonical_engine_count: int,
) -> dict[str, Any]:
    hard_rows = [
        {
            "gate": name,
            "passed": hard_gates.get(name, False) is True,
        }
        for name in HARD_GATES
    ]
    critical_rows = [
        {
            "case": name,
            "passed": critical_regressions.get(name, False) is True,
        }
        for name in CRITICAL_REGRESSIONS
    ]
    primary_improvement = primary_quality.get("meaningful_improvement") is True
    holdout_protected = holdout_use_count == 1
    one_canonical_engine = canonical_engine_count == 1
    checks = {
        "all_hard_gates": all(row["passed"] for row in hard_rows),
        "all_critical_regressions": all(row["passed"] for row in critical_rows),
        "primary_quality_improved": primary_improvement,
        "secondary_metrics_safe": secondary_no_material_regression,
        "cross_channel_generalization": cross_channel_generalization,
        "holdout_evaluated_exactly_once": holdout_protected,
        "one_canonical_engine": one_canonical_engine,
    }
    approved = all(checks.values())
    return {
        "artifact_version": "canonical-replacement-gates-v1",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "hard_gates": hard_rows,
        "critical_regressions": critical_rows,
        "primary_quality": primary_quality,
        "checks": checks,
        "replacement_approved": approved,
        "decision": (
            "canonical_replacement_approved"
            if approved
            else "replacement_blocked"
        ),
        "blocking_reasons": [
            name for name, passed in checks.items() if not passed
        ],
    }
