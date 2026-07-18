from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

import numpy as np

TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002700-\U000027bf]",
    flags=re.UNICODE,
)


def caption_features(caption: str) -> dict[str, Any]:
    words = TOKEN_RE.findall(caption)
    lowered = [word.lower() for word in words]
    first_person = {"i", "me", "my", "mine", "we", "us", "our", "ours"}
    second_person = {"you", "your", "yours"}
    calls_to_action = {"comment", "tell", "pick", "choose", "vote", "share", "watch"}
    letters = [character for character in caption if character.isalpha()]
    uppercase = sum(character.isupper() for character in letters)
    punctuation = Counter(character for character in caption if character in "?!.,:;—-")
    return {
        "character_count": len(caption),
        "word_count": len(words),
        "has_question": "?" in caption,
        "has_exclamation": "!" in caption,
        "emoji_count": len(EMOJI_RE.findall(caption)),
        "uppercase_ratio": round(uppercase / max(len(letters), 1), 6),
        "punctuation": dict(sorted(punctuation.items())),
        "opening": " ".join(lowered[:3]),
        "first_person_count": sum(word in first_person for word in lowered),
        "second_person_count": sum(word in second_person for word in lowered),
        "call_to_action": any(word in calls_to_action for word in lowered),
        "tokens": lowered,
    }


def aggregate_caption_features(captions: Iterable[str]) -> dict[str, Any]:
    rows = [caption_features(caption) for caption in captions if caption.strip()]
    if not rows:
        return {
            "sample_size": 0,
            "median_words": 0,
            "word_percentiles": {"p10": 0, "p25": 0, "p75": 0, "p90": 0},
        }
    word_counts = np.asarray([row["word_count"] for row in rows], dtype=float)
    character_counts = np.asarray([row["character_count"] for row in rows], dtype=float)
    openings = Counter(str(row["opening"]) for row in rows if row["opening"])
    ngrams: Counter[str] = Counter()
    for row in rows:
        tokens = list(row["tokens"])
        ngrams.update(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    return {
        "sample_size": len(rows),
        "median_words": round(float(np.median(word_counts)), 3),
        "median_characters": round(float(np.median(character_counts)), 3),
        "word_percentiles": {
            "p10": round(float(np.percentile(word_counts, 10)), 3),
            "p25": round(float(np.percentile(word_counts, 25)), 3),
            "p75": round(float(np.percentile(word_counts, 75)), 3),
            "p90": round(float(np.percentile(word_counts, 90)), 3),
        },
        "question_frequency": round(sum(row["has_question"] for row in rows) / len(rows), 6),
        "exclamation_frequency": round(sum(row["has_exclamation"] for row in rows) / len(rows), 6),
        "emoji_frequency": round(sum(row["emoji_count"] > 0 for row in rows) / len(rows), 6),
        "mean_uppercase_ratio": round(sum(row["uppercase_ratio"] for row in rows) / len(rows), 6),
        "first_person_frequency": round(
            sum(row["first_person_count"] > 0 for row in rows) / len(rows), 6
        ),
        "second_person_frequency": round(
            sum(row["second_person_count"] > 0 for row in rows) / len(rows), 6
        ),
        "call_to_action_frequency": round(
            sum(row["call_to_action"] for row in rows) / len(rows), 6
        ),
        "common_openings": openings.most_common(10),
        "repeated_bigrams": [item for item in ngrams.most_common(15) if item[1] > 1],
    }


def text_embedding(text: str, dimensions: int = 128) -> np.ndarray:
    vector = np.zeros(dimensions, dtype=np.float32)
    tokens = TOKEN_RE.findall(text.lower())
    features = tokens + [f"{tokens[index]}_{tokens[index + 1]}" for index in range(len(tokens) - 1)]
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def text_similarity(first: str, second: str) -> float:
    a = text_embedding(first)
    b = text_embedding(second)
    return float(np.dot(a, b))


def qlob_style_score(caption: str, stats: dict[str, Any]) -> float:
    features = caption_features(caption)
    median = float(stats.get("median_words", 1))
    distance = abs(float(features["word_count"]) - median) / max(median, 1)
    length_score = math.exp(-distance)
    question_target = float(stats.get("question_frequency", 0))
    question_score = 1.0 - abs(float(features["has_question"]) - question_target)
    exclamation_target = float(stats.get("exclamation_frequency", 0))
    exclamation_score = 1.0 - abs(float(features["has_exclamation"]) - exclamation_target)
    return round(
        max(0.0, min(1.0, 0.6 * length_score + 0.2 * question_score + 0.2 * exclamation_score)), 6
    )
