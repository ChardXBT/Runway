from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from runway.analysis.schemas import CaptionCandidate
from runway.captions.planning import EditorialBrief

QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{2,})[\"“”']")
PROPER_NOUN_RE = re.compile(r"\b(?:[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)*)\b")
UNSUPPORTED_EVENT_MARKERS = {
    "episode",
    "season finale",
    "after the",
    "before the",
    "just won",
    "just lost",
    "scored",
    "announced",
    "launched",
    "was fired",
    "got married",
}
RELATIONSHIP_MARKERS = {
    "brother",
    "sister",
    "mother",
    "father",
    "wife",
    "husband",
    "girlfriend",
    "boyfriend",
    "teammate",
    "boss",
}
GENERIC_PATTERNS = {
    "what do you think",
    "comment below",
    "like and subscribe",
    "thoughts?",
}
EMOTION_GROUPS = {
    "excited": {"excited", "excitement", "eager", "thrilled", "delighted"},
    "surprised": {"surprised", "shocked", "astonished", "startled", "surprise"},
    "angry": {"angry", "furious", "annoyed", "irritated"},
    "sad": {"sad", "upset", "unhappy", "dejected"},
    "worried": {"worried", "anxious", "nervous", "afraid"},
    "confused": {"confused", "puzzled", "bewildered"},
    "happy": {"happy", "joyful", "delighted"},
    "confident": {"confidence", "confident", "determination", "determined"},
}
COMMON_SENTENCE_STARTS = {
    "a",
    "an",
    "can",
    "choose",
    "every",
    "everyone",
    "how",
    "introducing",
    "is",
    "pick",
    "that",
    "the",
    "this",
    "what",
    "when",
    "why",
}


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    grounding_score: float = Field(ge=0, le=1)
    policy_score: float = Field(ge=0, le=1)
    supported_claims: list[str]
    unsupported_claims: list[str]
    warnings: list[str]
    checks: dict[str, bool]


class CaptionVerifier:
    def verify(
        self,
        candidate: CaptionCandidate,
        brief: EditorialBrief,
    ) -> VerificationResult:
        text = candidate.text.strip()
        lowered = text.casefold()
        visible_values = {self._normalized(fact.value): fact for fact in brief.visible_facts}
        visible_text = " ".join(visible_values)
        uncertain_text = " ".join(self._normalized(fact.value) for fact in brief.uncertain_facts)
        unsupported: list[str] = []
        warnings: list[str] = []
        supported: list[str] = []

        for quote in QUOTED_RE.findall(text):
            normalized = self._normalized(quote)
            if normalized not in visible_text:
                unsupported.append(f"invented_quote:{quote}")
            else:
                supported.append(f"visible_quote:{quote}")

        for marker in UNSUPPORTED_EVENT_MARKERS:
            if marker in lowered and marker not in visible_text:
                unsupported.append(f"invented_event:{marker}")

        relationship_facts = {
            self._normalized(fact.value)
            for fact in brief.visible_facts
            if fact.field == "relationship"
        }
        for marker in RELATIONSHIP_MARKERS:
            if marker in lowered and not any(marker in value for value in relationship_facts):
                unsupported.append(f"unsupported_relationship:{marker}")

        supported_entities = {self._normalized(value) for value in brief.supported_entity_names}
        for phrase in PROPER_NOUN_RE.findall(text):
            normalized = self._normalized(phrase)
            first = normalized.split()[0] if normalized else ""
            if first in COMMON_SENTENCE_STARTS:
                continue
            if normalized not in supported_entities and not any(
                normalized in entity or entity in normalized for entity in supported_entities
            ):
                unsupported.append(f"unsupported_entity:{phrase}")
            else:
                supported.append(f"visible_entity:{phrase}")

        visible_emotions = {
            self._canonical_emotion(fact.value)
            for fact in brief.visible_facts
            if fact.field == "emotion"
        }
        uncertain_emotions = {
            self._canonical_emotion(fact.value)
            for fact in brief.uncertain_facts
            if fact.field == "emotion"
        }
        mentioned_emotions = {
            canonical
            for canonical, words in EMOTION_GROUPS.items()
            if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)
        }
        for emotion in mentioned_emotions:
            if emotion in visible_emotions:
                supported.append(f"visible_emotion:{emotion}")
            elif emotion in uncertain_emotions:
                unsupported.append(f"low_confidence_emotion:{emotion}")
            else:
                unsupported.append(f"unsupported_emotion:{emotion}")

        if any(pattern in lowered for pattern in GENERIC_PATTERNS):
            unsupported.append("policy:generic_engagement_bait")
        word_count = len(re.findall(r"[\w']+", text, flags=re.UNICODE))
        minimum = brief.target_length["minimum_words"]
        maximum = brief.target_length["maximum_words"]
        if word_count < minimum or word_count > maximum:
            warnings.append(f"length_outside_channel_range:{word_count}/{minimum}-{maximum}")
        if candidate.language not in {"und", brief.target_language}:
            unsupported.append(f"language_mismatch:{candidate.language}!={brief.target_language}")
        if not candidate.visible_evidence:
            warnings.append("generator_supplied_no_candidate_evidence")
        else:
            invalid_evidence = [
                value
                for value in candidate.visible_evidence
                if self._normalized(value) not in visible_text
                and self._normalized(value) not in uncertain_text
            ]
            if invalid_evidence:
                warnings.append("unverified_candidate_evidence:" + ",".join(invalid_evidence))
        unsupported = list(dict.fromkeys(unsupported))
        supported = list(dict.fromkeys(supported))
        grounding_score = max(0.0, 1.0 - 0.28 * len(unsupported))
        if not supported and any(
            fact.field in {"entity", "emotion", "action", "object", "relationship"}
            for fact in brief.visible_facts
        ):
            grounding_score = min(grounding_score, 0.72)
        policy_failures = [value for value in unsupported if value.startswith("policy:")]
        policy_score = max(0.0, 1.0 - 0.5 * len(policy_failures))
        critical = any(
            value.startswith(
                (
                    "invented_quote:",
                    "invented_event:",
                    "unsupported_entity:",
                    "low_confidence_emotion:",
                    "unsupported_relationship:",
                )
            )
            for value in unsupported
        )
        passed = not critical and not policy_failures and grounding_score >= 0.72
        return VerificationResult(
            passed=passed,
            grounding_score=round(grounding_score, 6),
            policy_score=round(policy_score, 6),
            supported_claims=supported,
            unsupported_claims=unsupported,
            warnings=warnings,
            checks={
                "entities": not any(
                    value.startswith("unsupported_entity:") for value in unsupported
                ),
                "events": not any(value.startswith("invented_event:") for value in unsupported),
                "quotes": not any(value.startswith("invented_quote:") for value in unsupported),
                "relationships": not any(
                    value.startswith("unsupported_relationship:") for value in unsupported
                ),
                "policy": not policy_failures,
                "language": not any(
                    value.startswith("language_mismatch:") for value in unsupported
                ),
            },
        )

    @staticmethod
    def _normalized(value: str) -> str:
        return " ".join(re.findall(r"[\w']+", value.casefold(), flags=re.UNICODE))

    @classmethod
    def _canonical_emotion(cls, value: str) -> str:
        normalized = cls._normalized(value)
        words = set(normalized.split())
        for canonical, markers in EMOTION_GROUPS.items():
            if words & markers:
                return canonical
        return normalized


def verification_pass_rate(rows: Iterable[VerificationResult]) -> float:
    values = list(rows)
    return sum(row.passed for row in values) / len(values) if values else 0.0
