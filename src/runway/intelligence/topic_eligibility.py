from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from runway.analysis.schemas import CandidateAnalysis
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ChannelPolicyRule, StyleProfile
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import ActiveRepresentationResolver

TopicEligibilityClass = Literal[
    "supported",
    "adjacent",
    "exploratory",
    "off-topic",
    "blocked",
]


class TopicEligibilityResult(BaseModel):
    classification: TopicEligibilityClass
    semantic_score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    related_topic_distance: float = Field(ge=0, le=2)
    rank_penalty: float = Field(ge=0, le=1)
    hard_reject: bool
    candidate_topics: list[str]
    supported_topics: list[str]
    entity_aliases: dict[str, str]
    nearest_topics: list[dict[str, object]]
    reason: str
    representation: dict[str, object]


class TopicEligibilityService:
    """Channel-generic semantic topic gate with explicit policy hard blocks."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.settings = settings
        self.resolver = resolver or ActiveRepresentationResolver(database)

    def evaluate(self, analysis: CandidateAnalysis) -> TopicEligibilityResult:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            profile = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version), desc(StyleProfile.id))
                .limit(1)
            )
            policy_rows = session.scalars(
                select(ChannelPolicyRule).where(
                    ChannelPolicyRule.channel_id == channel_id,
                    ChannelPolicyRule.active.is_(True),
                    ChannelPolicyRule.rule_type.in_(("blocked_topic", "topic_exploration")),
                )
            ).all()

        supported = self._profile_topics(profile.profile_json if profile is not None else "{}")
        if supported:
            supported = sorted(
                {
                    *supported,
                    *(
                        self._normalize(value)
                        for value in self.settings.discovery_secondary_topic_list
                    ),
                }
            )
        candidates, aliases = self._candidate_topics(analysis)
        blocked_topics: set[str] = set()
        exploration_enabled = True
        for row in policy_rows:
            try:
                value = json.loads(row.value_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            if row.rule_type == "blocked_topic":
                blocked_topics.update(
                    self._normalize(str(item))
                    for item in value.get("topics", [])
                    if str(item).strip()
                )
            elif row.rule_type == "topic_exploration":
                exploration_enabled = bool(value.get("enabled", True))

        blocked_match = sorted(set(candidates) & blocked_topics)
        raw_provider, resolution = self.resolver.resolve(channel_id, modality="text")
        del raw_provider
        if blocked_match:
            return TopicEligibilityResult(
                classification="blocked",
                semantic_score=0.0,
                confidence=max(analysis.confidence, 0.9),
                related_topic_distance=1.0,
                rank_penalty=1.0,
                hard_reject=True,
                candidate_topics=candidates,
                supported_topics=supported,
                entity_aliases=aliases,
                nearest_topics=[],
                reason="explicit creator policy blocked: " + ", ".join(blocked_match),
                representation=resolution.as_dict(),
            )

        nearest: list[dict[str, object]] = []
        exact = bool(set(candidates) & set(supported))
        if exact:
            semantic_score = 1.0
        elif candidates and supported:
            for candidate in candidates:
                for topic in supported:
                    score = self.resolver.text_similarity(
                        channel_id,
                        candidate,
                        topic,
                        score_purpose="topic_eligibility",
                    ).score
                    nearest.append(
                        {
                            "candidate_topic": candidate,
                            "supported_topic": topic,
                            "score": round(score, 6),
                        }
                    )
            nearest.sort(
                key=lambda row: (
                    -self._score_value(row["score"]),
                    str(row["candidate_topic"]),
                    str(row["supported_topic"]),
                )
            )
            semantic_score = self._score_value(nearest[0]["score"]) if nearest else 0.0
        else:
            semantic_score = 0.5 if not supported else 0.0

        confidence = max(0.0, min(1.0, analysis.confidence))
        if not supported:
            classification: TopicEligibilityClass = "exploratory"
            reason = "the channel has no reliable supported-topic profile yet"
        elif exact or semantic_score >= 0.72:
            classification = "supported"
            reason = "candidate is an exact or high-similarity channel topic"
        elif semantic_score >= 0.48:
            classification = "adjacent"
            reason = "candidate is semantically adjacent to a supported channel topic"
        elif semantic_score >= 0.28 or confidence < 0.75:
            classification = "exploratory"
            reason = "candidate is unfamiliar but eligible for controlled exploration"
        elif exploration_enabled and semantic_score >= 0.12:
            classification = "exploratory"
            reason = "candidate is distant but inside the configured exploration boundary"
        else:
            classification = "off-topic"
            reason = "high-confidence candidate is semantically distant from channel topics"

        # Disabling exploration turns a confident, very distant item into off-topic;
        # low-confidence analysis still cannot justify a hard rejection.
        if (
            classification == "exploratory"
            and not exploration_enabled
            and semantic_score < 0.28
            and confidence >= 0.75
        ):
            classification = "off-topic"
            reason = "creator topic exploration is disabled and semantic distance is high"

        penalties = {
            "supported": 0.0,
            "adjacent": 0.06,
            "exploratory": 0.14,
            "off-topic": 0.35,
            "blocked": 1.0,
        }
        hard_reject = classification == "blocked" or (
            classification == "off-topic" and confidence >= 0.75
        )
        return TopicEligibilityResult(
            classification=classification,
            semantic_score=max(-1.0, min(1.0, semantic_score)),
            confidence=confidence,
            related_topic_distance=max(0.0, min(2.0, 1.0 - semantic_score)),
            rank_penalty=penalties[classification],
            hard_reject=hard_reject,
            candidate_topics=candidates,
            supported_topics=supported,
            entity_aliases=aliases,
            nearest_topics=nearest[:5],
            reason=reason,
            representation=resolution.as_dict(),
        )

    @staticmethod
    def _score_value(value: object) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0

    @classmethod
    def _candidate_topics(cls, analysis: CandidateAnalysis) -> tuple[list[str], dict[str, str]]:
        values: set[str] = set()
        aliases: dict[str, str] = {}
        if analysis.franchise:
            values.add(cls._normalize(analysis.franchise))
        for entity in analysis.entities:
            raw = cls._normalize(entity.name)
            canonical = cls._normalize(entity.canonical_name or entity.name)
            if raw:
                values.add(raw)
            if canonical:
                values.add(canonical)
            if raw and canonical and raw != canonical:
                aliases[raw] = canonical
        values.update(cls._normalize(value) for value in analysis.characters if value.strip())
        return sorted(value for value in values if value), dict(sorted(aliases.items()))

    @classmethod
    def _profile_topics(cls, raw: str) -> list[str]:
        try:
            profile = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(profile, dict):
            return []
        distribution = profile.get(
            "topic_distribution",
            profile.get("entity_distribution", profile.get("franchise_distribution", [])),
        )
        if not isinstance(distribution, list):
            return []
        statistics = profile.get("caption_statistics", {})
        sample_size = int(statistics.get("sample_size", 0)) if isinstance(statistics, dict) else 0
        minimum_support = max(2, round(sample_size * 0.01))
        topics: set[str] = set()
        for row in distribution:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                count = int(row[1])
            except (TypeError, ValueError):
                continue
            topic = cls._normalize(str(row[0]))
            if topic not in {"", "unknown", "none", "null"} and count >= minimum_support:
                topics.add(topic)
        return sorted(topics)

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.strip().casefold().split())
