from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import desc, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ChannelPolicyRule
from runway.intelligence.embeddings import configuration_hash


class PolicySnapshot(BaseModel):
    channel_id: int
    version: str
    rules: list[dict[str, object]]
    preferred_structures: list[str]
    prohibited_claims: list[str]
    language: str
    locale: str
    rights_policy: str
    source_policy: str
    question_first: bool


class PolicyDecision(BaseModel):
    outcome: Literal["allowed", "requires_review", "blocked"]
    reason: str
    policy_version: str


class ChannelPolicyService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def ensure_defaults(self, channel_id: int) -> PolicySnapshot:
        with self.database.session() as session:
            exists = session.scalar(
                select(ChannelPolicyRule.id)
                .where(
                    ChannelPolicyRule.channel_id == channel_id,
                    ChannelPolicyRule.active.is_(True),
                )
                .limit(1)
            )
        if exists is None:
            structures = (
                ["open_question", "observation", "reaction"]
                if self.settings.caption_question_first
                else ["observation", "reaction", "open_question"]
            )
            defaults: list[tuple[str, int, dict[str, object], str]] = [
                (
                    "caption_structure",
                    200,
                    {
                        "preferred": structures,
                        "question_first": self.settings.caption_question_first,
                    },
                    "Initial creator-channel structure preference from local configuration.",
                ),
                (
                    "language",
                    200,
                    {"language": "en", "locale": "en-CA"},
                    "Initial local language preference.",
                ),
                (
                    "rights",
                    300,
                    {"policy": "provenance_only"},
                    "Copyright and licensing are retained as provenance, not ranking vetoes.",
                ),
                (
                    "source",
                    300,
                    {"policy": "public_web_nsfw_blocked"},
                    "Public-web sources are eligible; explicit NSFW material is blocked.",
                ),
                (
                    "grounding",
                    400,
                    {
                        "prohibited_claims": [
                            "unsupported_entity",
                            "invented_event",
                            "invented_quote",
                            "unsupported_relationship",
                        ]
                    },
                    "Visible evidence is required for definitive claims.",
                ),
            ]
            for rule_type, priority, value, text in defaults:
                self.add_rule(
                    channel_id=channel_id,
                    rule_type=rule_type,
                    priority=priority,
                    value=value,
                    rule_text=text,
                    source="configuration",
                )
        return self.current(channel_id)

    def add_rule(
        self,
        *,
        channel_id: int,
        rule_type: str,
        priority: int,
        value: dict[str, object],
        rule_text: str | None = None,
        scope: str = "channel",
        source: str = "creator",
    ) -> ChannelPolicyRule:
        identity = configuration_hash(
            {
                "channel_id": channel_id,
                "rule_type": rule_type,
                "scope": scope,
                "priority": priority,
                "value": value,
                "text": rule_text,
                "source": source,
            }
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(ChannelPolicyRule)
                .where(
                    ChannelPolicyRule.channel_id == channel_id,
                    ChannelPolicyRule.version_hash == identity,
                    ChannelPolicyRule.active.is_(True),
                )
                .limit(1)
            )
            if existing is not None:
                return existing
            record = ChannelPolicyRule(
                channel_id=channel_id,
                rule_type=rule_type.strip(),
                scope=scope.strip(),
                priority=priority,
                value_json=json.dumps(value, sort_keys=True),
                rule_text=rule_text.strip() if rule_text else None,
                source=source.strip(),
                version_hash=identity,
                active=True,
            )
            session.add(record)
            session.flush()
            return record

    def current(self, channel_id: int) -> PolicySnapshot:
        now = datetime.now(UTC)
        with self.database.session() as session:
            records = session.scalars(
                select(ChannelPolicyRule)
                .where(
                    ChannelPolicyRule.channel_id == channel_id,
                    ChannelPolicyRule.active.is_(True),
                    (
                        (ChannelPolicyRule.starts_at.is_(None))
                        | (ChannelPolicyRule.starts_at <= now)
                    ),
                    ((ChannelPolicyRule.ends_at.is_(None)) | (ChannelPolicyRule.ends_at > now)),
                )
                .order_by(
                    desc(ChannelPolicyRule.priority),
                    ChannelPolicyRule.created_at,
                    ChannelPolicyRule.id,
                )
            ).all()
        if not records:
            raise LookupError(f"channel {channel_id} has no policy rules")
        rows: list[dict[str, object]] = []
        merged: dict[str, dict[str, object]] = {}
        for record in records:
            value = cast(dict[str, object], json.loads(record.value_json))
            rows.append(
                {
                    "id": record.id,
                    "type": record.rule_type,
                    "scope": record.scope,
                    "priority": record.priority,
                    "value": value,
                    "text": record.rule_text,
                    "source": record.source,
                    "version_hash": record.version_hash,
                }
            )
            merged.setdefault(record.rule_type, value)
        version = configuration_hash(
            [
                {
                    "id": row["id"],
                    "version_hash": row["version_hash"],
                    "priority": row["priority"],
                }
                for row in rows
            ]
        )
        structure = merged.get("caption_structure", {})
        language = merged.get("language", {})
        grounding = merged.get("grounding", {})
        return PolicySnapshot(
            channel_id=channel_id,
            version=version,
            rules=rows,
            preferred_structures=[
                str(value)
                for value in cast(
                    list[Any],
                    structure.get(
                        "preferred",
                        ["observation", "reaction", "open_question"],
                    ),
                )
            ],
            prohibited_claims=[
                str(value)
                for value in cast(
                    list[Any],
                    grounding.get(
                        "prohibited_claims",
                        [
                            "unsupported_entity",
                            "invented_event",
                            "invented_quote",
                        ],
                    ),
                )
            ],
            language=str(language.get("language", "und")),
            locale=str(language.get("locale", "und")),
            rights_policy=str(merged.get("rights", {}).get("policy", "unknown_blocked")),
            source_policy=str(merged.get("source", {}).get("policy", "preserve_and_review")),
            question_first=bool(structure.get("question_first", False)),
        )

    def rights_decision(
        self,
        *,
        channel_id: int,
        rights_status: str,
        explicitly_approved: bool = False,
        for_generation: bool = False,
    ) -> PolicyDecision:
        policy = self.ensure_defaults(channel_id)
        normalized = rights_status.strip().lower()
        if policy.rights_policy == "provenance_only":
            return PolicyDecision(
                outcome="allowed",
                reason=f"{normalized or 'unknown'} retained as provenance only",
                policy_version=policy.version,
            )
        if normalized == "blocked":
            return PolicyDecision(
                outcome="blocked",
                reason="rights status is explicitly blocked",
                policy_version=policy.version,
            )
        if normalized in {"creator_owned", "licensed", "public_domain"}:
            return PolicyDecision(
                outcome="allowed",
                reason=f"{normalized} is eligible under the channel policy",
                policy_version=policy.version,
            )
        if explicitly_approved:
            return PolicyDecision(
                outcome="allowed",
                reason="the creator explicitly approved this reference",
                policy_version=policy.version,
            )
        if for_generation:
            return PolicyDecision(
                outcome="blocked",
                reason="unknown-rights media cannot be used as a generation reference",
                policy_version=policy.version,
            )
        if policy.rights_policy == "unknown_requires_review":
            return PolicyDecision(
                outcome="requires_review",
                reason="unknown rights require the creator's explicit editorial decision",
                policy_version=policy.version,
            )
        return PolicyDecision(
            outcome="blocked",
            reason="unknown rights are blocked by channel policy",
            policy_version=policy.version,
        )

    def configure_public_image_policy(self, channel_id: int) -> PolicySnapshot:
        self.ensure_defaults(channel_id)
        self.add_rule(
            channel_id=channel_id,
            rule_type="rights",
            priority=1000,
            value={"policy": "provenance_only"},
            rule_text="Copyright and licensing status do not veto image discovery or ranking.",
            source="creator",
        )
        self.add_rule(
            channel_id=channel_id,
            rule_type="source",
            priority=1000,
            value={"policy": "public_web_nsfw_blocked"},
            rule_text=(
                "Any public-web image source is eligible; explicit sexual or NSFW material "
                "remains prohibited."
            ),
            source="creator",
        )
        return self.current(channel_id)
