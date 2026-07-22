from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from runway.captions.planning import EditorialBrief
from runway.captions.verification import VerificationResult

ClaimClass = Literal[
    "visible",
    "supported_by_context",
    "contradicted",
    "unsupported",
    "uncertain",
    "stylistic_non_factual",
]


class AtomicClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    classification: ClaimClass
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    critical: bool
    verifier_provider: str
    verifier_model: str
    prompt_version: str


class SemanticClaimVerifier(Protocol):
    provider: str
    model: str
    prompt_version: str

    def verify(
        self,
        *,
        claims: list[str],
        image_path: str,
        allowed_context: dict[str, object],
    ) -> list[AtomicClaim]: ...


@dataclass(frozen=True)
class ClaimVerificationBundle:
    claims: list[AtomicClaim]
    passed: bool
    critical_failures: list[str]
    unsupported_claim_rate: float
    semantic_layer_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "claims": [claim.model_dump() for claim in self.claims],
            "passed": self.passed,
            "critical_failures": self.critical_failures,
            "unsupported_claim_rate": self.unsupported_claim_rate,
            "semantic_layer_status": self.semantic_layer_status,
        }


class ClaimLevelGroundingVerifier:
    """Atomic-claim layer over the fast verifier, with an offline VLM boundary."""

    prompt_version = "claim-grounding-v1"
    critical_markers = (
        "invented_event",
        "invented_quote",
        "unsupported_entity",
        "unsupported_relationship",
        "low_confidence_emotion",
    )

    def __init__(self, semantic_verifier: SemanticClaimVerifier | None = None):
        self.semantic_verifier = semantic_verifier

    def verify(
        self,
        *,
        caption: str,
        brief: EditorialBrief,
        first_layer: VerificationResult,
        image_path: str,
    ) -> ClaimVerificationBundle:
        extracted = self.extract(caption)
        allowed_context: dict[str, object] = {
            "visible_facts": [fact.model_dump() for fact in brief.visible_facts],
            "uncertain_facts": [fact.model_dump() for fact in brief.uncertain_facts],
            "allowed_context": {
                "positive_evidence": brief.positive_evidence,
                "explicit_rules": brief.explicit_rules,
                "source_context": brief.source_context,
            },
            "prohibited_claims": brief.prohibited_claims,
        }
        if self.semantic_verifier is not None:
            claims = self.semantic_verifier.verify(
                claims=extracted,
                image_path=image_path,
                allowed_context=allowed_context,
            )
            status = "semantic_verifier_completed"
        else:
            claims = self._from_fast_layer(extracted, first_layer, brief)
            status = "semantic_verifier_inactive_missing_evaluated_local_vlm"
        critical_failures = [
            claim.claim
            for claim in claims
            if claim.critical and claim.classification in {"contradicted", "unsupported"}
        ]
        factual = [claim for claim in claims if claim.classification != "stylistic_non_factual"]
        unsupported = sum(
            claim.classification in {"contradicted", "unsupported"} for claim in factual
        )
        rate = unsupported / len(factual) if factual else 0.0
        return ClaimVerificationBundle(
            claims=claims,
            passed=first_layer.passed and not critical_failures,
            critical_failures=critical_failures,
            unsupported_claim_rate=round(rate, 6),
            semantic_layer_status=status,
        )

    @staticmethod
    def extract(caption: str) -> list[str]:
        text = " ".join(caption.strip().split())
        if not text:
            return []
        clauses = [
            part.strip(" ,;:!?—-")
            for part in re.split(r"(?<=[.!?])\s+|\s+(?:and|but|while|because)\s+", text)
            if part.strip(" ,;:!?—-")
        ]
        return list(dict.fromkeys(clauses))

    def _from_fast_layer(
        self,
        claims: list[str],
        first_layer: VerificationResult,
        brief: EditorialBrief,
    ) -> list[AtomicClaim]:
        unsupported = list(first_layer.unsupported_claims)
        supported = list(first_layer.supported_claims)
        visible_terms = {
            self._normalize(fact.value) for fact in brief.visible_facts if fact.value.strip()
        }
        uncertain_terms = {
            self._normalize(fact.value) for fact in brief.uncertain_facts if fact.value.strip()
        }
        results: list[AtomicClaim] = []
        for claim in claims:
            normalized = self._normalize(claim)
            question_or_style = claim.rstrip().endswith("?") or not re.search(
                r"\b(?:is|are|was|were|has|have|won|lost|did|will|finally)\b",
                normalized,
            )
            critical_evidence = [
                item for item in unsupported if item.startswith(self.critical_markers)
            ]
            if critical_evidence:
                classification: ClaimClass = "unsupported"
                confidence = 0.9
                evidence = critical_evidence
                critical = True
            elif unsupported:
                classification = "unsupported"
                confidence = 0.75
                evidence = unsupported
                critical = False
            elif any(term and term in normalized for term in visible_terms):
                classification = "visible"
                confidence = 0.85
                evidence = supported or ["high_confidence_visual_fact"]
                critical = False
            elif any(term and term in normalized for term in uncertain_terms):
                classification = "uncertain"
                confidence = 0.6
                evidence = ["low_confidence_visual_fact"]
                critical = False
            elif question_or_style:
                classification = "stylistic_non_factual"
                confidence = 0.8
                evidence = ["non_declarative_or_editorial_language"]
                critical = False
            else:
                classification = "supported_by_context"
                confidence = 0.65
                evidence = supported or ["fast_verifier_passed"]
                critical = False
            results.append(
                AtomicClaim(
                    claim=claim,
                    classification=classification,
                    confidence=confidence,
                    evidence=evidence,
                    critical=critical,
                    verifier_provider="runway-deterministic",
                    verifier_model="caption-verifier-fast-layer",
                    prompt_version=self.prompt_version,
                )
            )
        return results

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(re.findall(r"[\w']+", value.casefold()))
