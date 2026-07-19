from __future__ import annotations

from typing import Any, cast

from runway.evaluation.datasets import CaseLabel

DEFAULT_WEIGHTS = {
    "preference": 0.20,
    "grounding": 0.22,
    "policy": 0.12,
    "style": 0.11,
    "novelty": 0.09,
    "rotation": 0.06,
    "positive_feedback": 0.06,
    "pairing": 0.09,
    "negative_feedback_risk": -0.08,
    "generic_penalty": -0.12,
}


def candidate_score(
    candidate: dict[str, Any],
    weights: dict[str, float],
    *,
    disabled_components: set[str] | None = None,
) -> float:
    disabled = disabled_components or set()
    components = cast(dict[str, Any], candidate.get("components", {}))

    def value(name: str) -> float:
        if name in disabled:
            return 0.0
        raw = components.get(name, candidate.get(name, 0.0))
        return float(raw) if isinstance(raw, (int, float)) else 0.0

    score = sum(
        weight * value(name) for name, weight in weights.items() if name != "generic_penalty"
    )
    if "generic_penalty" not in disabled:
        score += weights.get("generic_penalty", 0.0) * float(candidate.get("generic_penalty", 0.0))
    score += 0.08 * value("structure_fit")
    score += 0.04 * value("length_fit")
    return max(0.0, min(1.0, score))


def select_candidate(
    candidates: list[dict[str, Any]],
    weights: dict[str, float],
    *,
    disabled_components: set[str] | None = None,
) -> dict[str, Any] | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("eligible", True) is not False
        and bool(
            cast(dict[str, Any], candidate.get("verification", {})).get(
                "passed",
                True,
            )
        )
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            -candidate_score(
                candidate,
                weights,
                disabled_components=disabled_components,
            ),
            -float(
                cast(dict[str, Any], candidate.get("components", {})).get(
                    "grounding",
                    0.0,
                )
            ),
            str(candidate.get("text", "")),
        ),
    )


def evaluate_selection(
    candidate: dict[str, Any] | None,
    label: CaseLabel,
) -> dict[str, object]:
    if candidate is None:
        return {
            "case_id": label.case_id,
            "abstained": True,
            "accepted_proxy": False,
            "pairwise_correct_proxy": False,
            "grounded": False,
            "unsupported_claims": 0,
        }
    text = str(candidate.get("text", ""))
    normalized = text.casefold()
    structure = str(candidate.get("structure", ""))
    verifier = cast(dict[str, Any], candidate.get("verification", {}))
    unsupported = cast(list[object], verifier.get("unsupported_claims", []))
    expected_term_hit = any(term.casefold() in normalized for term in label.expected_terms)
    forbidden_hit = any(term.casefold() in normalized for term in label.forbidden_terms)
    structure_hit = structure in label.preferred_structures
    grounded = bool(verifier.get("passed", False)) and not unsupported
    accepted = grounded and structure_hit and expected_term_hit and not forbidden_hit
    return {
        "case_id": label.case_id,
        "selected_text": text,
        "selected_structure": structure,
        "abstained": False,
        "accepted_proxy": accepted,
        "pairwise_correct_proxy": structure_hit and expected_term_hit,
        "expected_term_hit": expected_term_hit,
        "forbidden_term_hit": forbidden_hit,
        "grounded": grounded,
        "grounding_score": float(verifier.get("grounding_score", 0.0)),
        "unsupported_claims": len(unsupported),
    }


def aggregate_case_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    count = len(rows)
    accepted = sum(bool(row["accepted_proxy"]) for row in rows)
    pairwise = sum(bool(row["pairwise_correct_proxy"]) for row in rows)
    grounded = sum(bool(row["grounded"]) for row in rows)
    abstained = sum(bool(row["abstained"]) for row in rows)
    unsupported = sum(
        int(value)
        for row in rows
        if isinstance((value := row["unsupported_claims"]), (int, float, str))
    )
    return {
        "case_count": count,
        "no_edit_acceptance_proxy": round(accepted / count, 6) if count else 0.0,
        "pairwise_preference_accuracy_proxy": (round(pairwise / count, 6) if count else 0.0),
        "grounding_pass_rate": round(grounded / count, 6) if count else 0.0,
        "unsupported_claim_rate": round(unsupported / count, 6) if count else 0.0,
        "abstention_rate": round(abstained / count, 6) if count else 0.0,
        "objective": round(
            (0.45 * accepted + 0.35 * pairwise + 0.20 * grounded - 0.35 * unsupported) / count,
            6,
        )
        if count
        else 0.0,
        "label_scope": "deterministic_fixture_policy_proxy_not_human_preference",
    }
