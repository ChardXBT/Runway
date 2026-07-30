from __future__ import annotations

import re

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
UNRESOLVED_VISUAL_PLACEHOLDER_RE = re.compile(
    r"(?:\bso\s+(?:unknown|unclear|unspecified)\b|"
    r"\b(?:unknown|unclear|unspecified|unidentified)\s+"
    r"(?:action|emotion|expression|feeling|look|mood|reaction)\b)",
    re.IGNORECASE,
)
GENERIC_QUESTION_PATTERNS = (
    re.compile(r"^what (?:is|was) .{1,48} thinking about\??$", re.IGNORECASE),
    re.compile(r"^what(?:'s| is) (?:going on|happening)(?: here)?\??$", re.IGNORECASE),
    re.compile(
        (
            r"^what(?:'s| is) (?:going on|happening) "
            r"(?:at|by|near|around) (?:that|this|the) .{1,48}\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(r"^what do you make of this\??$", re.IGNORECASE),
    re.compile(
        r"^what (?:detail )?(?:stands out|catches your eye)(?: first)?(?: here)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:what|which) detail did you (?:notice|spot)(?: first)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^what did you (?:notice|spot)(?: first)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^which detail catches your eye(?: first)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^(?:what|which) detail (?:feels|looks|seems) "
            r"(?:the )?(?:most )?"
            r"(?:interesting|important|mysterious|odd|strange|surprising|unusual)"
            r"(?: here)?\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^(?:why|how) (?:is|was) "
            r"(?:he|she|it|the (?:person|character|figure)) "
            r"(?:standing|sitting|looking|waiting)(?: here| there)?\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^(?:why|how) (?:are|were) "
            r"(?:they|the (?:people|characters|figures)) "
            r"(?:standing|sitting|looking|waiting)(?: here| there)?\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^what (?:is|was) (?:he|she|it|the (?:person|character|figure)) "
            r"doing(?: here| there)?\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        r"^what (?:are|were) (?:they|the (?:people|characters|figures)) doing"
        r"(?: here| there)?\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^which (?:face|expression|reaction) contrasts (?:the )?most with\b.*\??$",
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^(?:what|which) (?:visible )?(?:detail|part|thing) makes "
            r".{1,48} (?:look|seem|feel) (?:unusual|strange|different|odd)\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^why is (?:[a-z][a-z0-9'-]*(?: [a-z][a-z0-9'-]*){0,2}|he|she|it) "
            r"looking (?:left|right|up|down|away|here|there|over there)\??$"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"^why (?:is|are|was|were) "
            r"(?:[a-z][a-z0-9'-]*(?: [a-z][a-z0-9'-]*){0,2}|he|she|it|they) "
            r"(?:still )?holding (?:a|an|his|her|their|that|the|this) "
            r"[a-z0-9'-]+(?: [a-z0-9'-]+)?\??$"
        ),
        re.IGNORECASE,
    ),
)
CLINICAL_SUBJECT_RE = re.compile(
    r"\b(?:some|several|these|those|the|other|animated|cartoon|standing|seated) figures?\b",
    re.IGNORECASE,
)
SPATIAL_SUBJECT_RE = re.compile(
    (
        r"\b(?:(?:left|right|foreground|background) (?:character|person|figure)|"
        r"(?:character|person|figure) (?:on|at|in) (?:the )?"
        r"(?:left|right|foreground|background))\b"
    ),
    re.IGNORECASE,
)
COMPOSITION_JARGON_RE = re.compile(
    r"\b(?:foregrounded|backgrounded|two[- ]shot|centered in (?:the )?frame)\b",
    re.IGNORECASE,
)
CLINICAL_SHAPE_OBJECT_RE = re.compile(
    r"\b(?:spherical|circular|rectangular|cylindrical) objects?\b",
    re.IGNORECASE,
)
GENERIC_ADJECTIVE_FILLER_RE = re.compile(
    r"^(?:a|an) (?:very )?(?:unusual|strange|odd|interesting) .{1,60}[.!]?$",
    re.IGNORECASE,
)
GENERIC_CONTRAST_FILLER_RE = re.compile(
    (
        r"^(?:this|the) frame (?:has|shows|uses) "
        r"(?:(?:a|some) )?(?:serious|strong|clear|high|visual)?\s*contrast[.!]?$"
    ),
    re.IGNORECASE,
)
INCOMPLETE_WHY_HOW_QUESTION_RE = re.compile(
    (
        r"^(?:why|how) (?:carry|hold|keep|leave|look|put|sit|stand|use|wait|wear)"
        r"\b.*\?$"
    ),
    re.IGNORECASE,
)
EMOTION_GROUPS = {
    "excited": {"excited", "excitement", "eager", "thrilled"},
    "surprised": {"surprised", "shocked", "astonished", "startled", "surprise"},
    "angry": {"angry", "furious", "annoyed", "irritated"},
    "sad": {"sad", "upset", "unhappy", "dejected"},
    "worried": {"worried", "anxious", "nervous", "afraid"},
    "confused": {"confused", "puzzled", "bewildered"},
    "happy": {"happy", "happiness", "joyful", "delight", "delighted"},
    "confident": {"confidence", "confident", "determination", "determined"},
}
COMMON_SENTENCE_STARTS = {
    "a",
    "an",
    "can",
    "choose",
    "could",
    "did",
    "do",
    "does",
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
    "which",
    "who",
    "why",
    "would",
    "should",
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

        object_facts = [
            self._normalized(fact.value)
            for fact in [*brief.visible_facts, *brief.uncertain_facts]
            if fact.field == "object"
        ]
        if (
            any("champagne flute" in value for value in object_facts)
            and re.search(r"\bflutes?\b", lowered)
            and not re.search(
                r"\b(?:champagne|drinking|sparkling(?: wine)?|wine)\s+flutes?\b"
                r"|\bflute(?:d)?\s+glasses?\b",
                lowered,
            )
        ):
            unsupported.append("policy:ambiguous_object_label:flute")

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

        if is_generic_engagement_bait(text):
            unsupported.append("policy:generic_engagement_bait")
        if CLINICAL_SUBJECT_RE.search(text) and not re.search(
            r"\b(?:action|collectible) figures?\b",
            text,
            re.IGNORECASE,
        ):
            unsupported.append("policy:clinical_subject_label:figure")
        if SPATIAL_SUBJECT_RE.search(text):
            unsupported.append("policy:clinical_subject_label:spatial")
        if COMPOSITION_JARGON_RE.search(text):
            unsupported.append("policy:composition_jargon")
        if CLINICAL_SHAPE_OBJECT_RE.search(text):
            unsupported.append("policy:clinical_object_label:shape")
        if GENERIC_ADJECTIVE_FILLER_RE.match(text):
            unsupported.append("policy:generic_adjective_filler")
        if GENERIC_CONTRAST_FILLER_RE.match(text):
            unsupported.append("policy:generic_contrast_filler")
        if INCOMPLETE_WHY_HOW_QUESTION_RE.match(text):
            unsupported.append("policy:incomplete_question")
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


def is_generic_engagement_bait(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    lowered = normalized.casefold()
    return any(pattern in lowered for pattern in GENERIC_PATTERNS) or any(
        pattern.fullmatch(normalized) for pattern in GENERIC_QUESTION_PATTERNS
    )


def has_unresolved_visual_placeholder(text: str) -> bool:
    """Reject model-internal uncertainty labels that escaped into audience copy."""

    return UNRESOLVED_VISUAL_PLACEHOLDER_RE.search(" ".join(text.split())) is not None
