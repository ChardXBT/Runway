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
ACTION_MARKERS = {
    "arguing": {"argue", "argues", "arguing", "argument"},
    "driving": {"drive", "drives", "driving", "drove"},
    "eating": {"eat", "eats", "eating", "ate"},
    "holding": {"hold", "holds", "holding", "held"},
    "kissing": {"kiss", "kisses", "kissing", "kissed"},
    "reading": {"read", "reads", "reading"},
    "running": {"run", "runs", "running", "ran"},
    "sleeping": {"sleep", "sleeps", "sleeping", "slept"},
    "talking": {"talk", "talks", "talking", "spoke", "speaking"},
    "watching": {"watch", "watches", "watching", "watched"},
    "writing": {"write", "writes", "writing", "wrote"},
}
EVIDENCE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "appears",
    "are",
    "at",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "visible",
    "while",
    "with",
}
EVIDENCE_ALIASES = {
    "annoyance": "annoyed",
    "boredom": "bored",
    "confidence": "confident",
    "crackers": "cracker",
    "determination": "confident",
    "determined": "confident",
    "eating": "eat",
    "eats": "eat",
    "held": "hold",
    "holding": "hold",
    "holds": "hold",
    "indoors": "indoor",
    "excitement": "excited",
    "happiness": "happy",
    "newspaper": "paper",
    "newspapers": "paper",
    "reading": "read",
    "reads": "read",
    "seated": "sit",
    "sitting": "sit",
    "sofa": "couch",
    "surprise": "surprised",
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

        visible_actions = " ".join(
            self._normalized(fact.value) for fact in brief.visible_facts if fact.field == "action"
        )
        for action, markers in ACTION_MARKERS.items():
            if not any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in markers):
                continue
            if not any(
                re.search(rf"\b{re.escape(marker)}\b", visible_actions) for marker in markers
            ):
                unsupported.append(f"unsupported_action:{action}")

        caption_tokens = self._evidence_tokens(text)
        visible_tokens_by_field: dict[str, list[set[str]]] = {}
        for fact in brief.visible_facts:
            visible_tokens_by_field.setdefault(fact.field, []).append(
                self._evidence_tokens(fact.value)
            )
        for fact in brief.uncertain_facts:
            if fact.field == "entity":
                continue
            disputed_tokens = self._evidence_tokens(fact.value)
            if not disputed_tokens or not disputed_tokens <= caption_tokens:
                continue
            if any(
                disputed_tokens <= visible_tokens
                for visible_tokens in visible_tokens_by_field.get(fact.field, [])
            ):
                continue
            unsupported.append(f"unsupported_disputed_fact:{fact.field}:{fact.value}")

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
                if not self._evidence_supported(
                    value,
                    [fact.value for fact in brief.visible_facts],
                    [fact.value for fact in brief.uncertain_facts],
                )
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
                    "unsupported_action:",
                    "unsupported_disputed_fact:",
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

    @classmethod
    def _evidence_supported(
        cls,
        evidence: str,
        visible_facts: list[str],
        uncertain_facts: list[str],
    ) -> bool:
        evidence_tokens = cls._evidence_tokens(evidence)
        if not evidence_tokens:
            return False
        visible_tokens = set().union(*(cls._evidence_tokens(value) for value in visible_facts))
        uncertain_tokens = set().union(*(cls._evidence_tokens(value) for value in uncertain_facts))
        coverage = len(evidence_tokens & visible_tokens) / len(evidence_tokens)
        # A disputed *specific* claim must not erase a separately confirmed
        # generic fact.  For example, independent passes can agree that Homer
        # is holding something while disagreeing over whether it is a wrapper
        # or a newspaper.  Only tokens that exist exclusively in the disputed
        # evidence should veto a candidate-provided grounding claim.
        uncertain_only_tokens = uncertain_tokens - visible_tokens
        uncertain_overlap = evidence_tokens & uncertain_only_tokens
        return coverage >= 0.72 and not uncertain_overlap

    @staticmethod
    def _evidence_tokens(value: str) -> set[str]:
        tokens: set[str] = set()
        for raw in re.findall(r"[a-z0-9']+", value.casefold()):
            if raw in EVIDENCE_STOP_WORDS:
                continue
            token = EVIDENCE_ALIASES.get(raw, raw)
            if token.endswith("s") and len(token) > 4:
                token = token[:-1]
            tokens.add(token)
        return tokens


def verification_pass_rate(rows: Iterable[VerificationResult]) -> float:
    values = list(rows)
    return sum(row.passed for row in values) / len(values) if values else 0.0
