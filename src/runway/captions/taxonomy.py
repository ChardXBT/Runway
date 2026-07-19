from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel

CaptionStructure = Literal[
    "open_question",
    "yes_no_question",
    "observation",
    "reaction",
    "comparison",
    "prediction",
    "fill_in_blank",
    "poll",
    "quiz",
    "call_to_action",
    "explanation",
    "promotional_statement",
    "quote_or_reference",
]

OPEN_QUESTION_RE = re.compile(
    r"^\s*(why|how|what|who|where|when|which|"
    r"por qué|cómo|qué|quién|dónde|cuándo|"
    r"pourquoi|comment|quoi|qui|où|quand)\b",
    re.IGNORECASE,
)
YES_NO_QUESTION_RE = re.compile(
    r"^\s*(is|are|was|were|do|does|did|can|could|would|will|has|have|"
    r"should|did|isn't|aren't)\b",
    re.IGNORECASE,
)


class CaptionTaxonomyResult(BaseModel):
    structure: CaptionStructure
    normalized_text: str
    is_question: bool
    open_question: bool
    language: str


def normalize_caption(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def analyze_caption(text: str, *, language: str = "und") -> CaptionTaxonomyResult:
    stripped = text.strip()
    lowered = stripped.casefold()
    is_question = stripped.endswith("?")
    open_question = bool(is_question and OPEN_QUESTION_RE.match(stripped))
    if open_question:
        structure: CaptionStructure = "open_question"
    elif is_question:
        structure = "yes_no_question" if YES_NO_QUESTION_RE.match(stripped) else "open_question"
    elif "___" in stripped or "____" in stripped:
        structure = "fill_in_blank"
    elif any(marker in lowered for marker in ("vote ", "poll:", "pick one")):
        structure = "poll"
    elif any(marker in lowered for marker in ("quiz:", "can you name", "guess the")):
        structure = "quiz"
    elif any(
        lowered.startswith(marker)
        for marker in ("try ", "watch ", "tell us ", "share ", "join ", "choose ")
    ):
        structure = "call_to_action"
    elif any(marker in lowered for marker in (" vs ", "versus", "compared with", "better than")):
        structure = "comparison"
    elif any(marker in lowered for marker in ("will ", "going to ", "next up", "prediction")):
        structure = "prediction"
    elif stripped.startswith(('"', "“", "'")) and stripped.endswith(('"', "”", "'")):
        structure = "quote_or_reference"
    elif any(
        marker in lowered for marker in ("now available", "introducing", "shop ", "launching")
    ):
        structure = "promotional_statement"
    elif any(
        marker in lowered
        for marker in (
            "that look",
            "the face of",
            "this should",
            "moments before",
            "when you",
            "me when",
        )
    ):
        structure = "reaction"
    elif len(stripped.split()) >= 12 and any(
        marker in lowered for marker in ("because", "means that", "works by", "here's how")
    ):
        structure = "explanation"
    else:
        structure = "observation"
    return CaptionTaxonomyResult(
        structure=structure,
        normalized_text=normalize_caption(stripped),
        is_question=is_question,
        open_question=open_question,
        language=language,
    )
