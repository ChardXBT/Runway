from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.captions.feature_snapshots import (
    FEATURE_SCHEMA_VERSION,
    TAXONOMY_VERSION,
    VERIFIER_VERSION,
    candidate_components,
    snapshot_json_and_hash,
)
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ActiveLearningBatch,
    ActiveLearningSelection,
    BlindStudy,
    BlindStudyCase,
    BlindStudyResponse,
    CandidateImage,
    CaptionCandidateRecord,
    CaptionSlate,
    GeneratedAssetLineage,
    MediaAsset,
    PairwisePreference,
    Post,
    PostMedia,
    SearchRun,
    utcnow,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import configuration_hash

StudySplit = Literal["development", "tuning", "final_holdout"]
PreferenceTarget = Literal["caption", "image", "pairing"]
RequestedTarget = Literal[
    "caption",
    "image",
    "pairing",
    "annotation",
    "retrieval_relevance",
]


class BlindStudyCasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=1, max_length=128)
    media_asset_id: int = Field(gt=0)
    candidate_image_id: int | None = Field(default=None, gt=0)
    group_key: str = Field(min_length=1, max_length=128)
    split: StudySplit
    baseline_caption: str = Field(min_length=1, max_length=5000)
    challenger_caption: str = Field(min_length=1, max_length=5000)
    baseline_candidate_id: int | None = Field(default=None, gt=0)
    challenger_candidate_id: int | None = Field(default=None, gt=0)
    metadata: dict[str, object] = Field(default_factory=dict)
    selection_rationale: dict[str, object] = Field(default_factory=dict)

    @field_validator("case_key", "group_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("study keys cannot be blank")
        return normalized


class BlindStudyImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=1, max_length=128)
    choice: Literal["first", "second", "tie"]
    acceptable_choices: list[Literal["first", "second"]] = Field(
        default_factory=list,
        max_length=2,
    )
    edited_final_caption: str | None = Field(default=None, max_length=5000)
    image_verdict: Literal["accepted", "rejected", "unsure"] | None = None
    caption_verdict: Literal["accepted", "rejected", "unsure"] | None = None
    pairing_verdict: Literal["accepted", "rejected", "unsure"] | None = None
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    note: str | None = Field(default=None, max_length=5000)
    decision_time_ms: float | None = Field(default=None, ge=0)
    started_at: datetime | None = None

    @field_validator("case_key")
    @classmethod
    def normalize_case_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("edited_final_caption")
    @classmethod
    def normalize_edit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ActiveLearningCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: int = Field(gt=0)
    group_key: str = Field(min_length=1, max_length=128)
    split: Literal["development", "tuning"]
    target: RequestedTarget
    uncertainty_score: float = Field(ge=0, le=1)
    disagreement_score: float = Field(default=0, ge=0, le=1)
    information_gain_score: float = Field(default=0, ge=0, le=1)
    diversity_cluster: str = Field(min_length=1, max_length=128)
    reasons: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, object] = Field(default_factory=dict)


class BlindStudyService:
    """Preregister, blind, export, and import genuine creator comparisons."""

    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def plan(
        self,
        cases: list[BlindStudyCasePlan],
        *,
        baseline_identity: str,
        challenger_identity: str,
        target: PreferenceTarget = "caption",
        seed: int = 20260718,
        maximum_cases_per_duplicate_cluster: int = 2,
    ) -> dict[str, object]:
        if not cases:
            raise ValueError("blind study planning requires at least one case")
        if not baseline_identity.strip() or not challenger_identity.strip():
            raise ValueError("baseline and challenger identities are required")
        if baseline_identity.strip() == challenger_identity.strip():
            raise ValueError("baseline and challenger identities must differ")
        case_keys = [case.case_key for case in cases]
        if len(set(case_keys)) != len(case_keys):
            raise ValueError("blind study case keys must be unique")
        media_ids = [case.media_asset_id for case in cases]
        if len(set(media_ids)) != len(media_ids):
            raise ValueError("blind study cases must use untouched, unique media")
        group_splits: dict[str, str] = {}
        for case in cases:
            prior = group_splits.setdefault(case.group_key, case.split)
            if prior != case.split:
                raise ValueError(f"group {case.group_key!r} crosses study splits")

        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            allowed_media = self._channel_media_ids(session, channel.id)
            missing_media = sorted(set(media_ids) - allowed_media)
            if missing_media:
                raise LookupError(
                    "study media are missing or outside the configured channel: "
                    + ", ".join(str(value) for value in missing_media)
                )
            media_rows = {
                row.id: row
                for row in session.scalars(select(MediaAsset).where(MediaAsset.id.in_(media_ids)))
            }
            candidate_ids = {
                case.candidate_image_id for case in cases if case.candidate_image_id is not None
            }
            candidates = {
                row.id: row
                for row in session.scalars(
                    select(CandidateImage)
                    .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                    .where(
                        SearchRun.channel_id == channel.id,
                        CandidateImage.id.in_(candidate_ids),
                    )
                )
            }
            caption_candidate_ids = {
                value
                for case in cases
                for value in (
                    case.baseline_candidate_id,
                    case.challenger_candidate_id,
                )
                if value is not None
            }
            caption_candidates = {
                row.id: row
                for row in session.scalars(
                    select(CaptionCandidateRecord).where(
                        CaptionCandidateRecord.channel_id == channel.id,
                        CaptionCandidateRecord.id.in_(caption_candidate_ids),
                    )
                )
            }
            channel_id = channel.id
        if candidate_ids - set(candidates):
            raise LookupError("a study candidate image is outside the configured channel")
        if caption_candidate_ids - set(caption_candidates):
            raise LookupError("a study caption candidate is outside the configured channel")
        for case in cases:
            if case.candidate_image_id is not None:
                candidate = candidates[case.candidate_image_id]
                if candidate.media_asset_id != case.media_asset_id:
                    raise ValueError(f"case {case.case_key} candidate and media do not match")
            for candidate_id, expected_text in (
                (case.baseline_candidate_id, case.baseline_caption),
                (case.challenger_candidate_id, case.challenger_caption),
            ):
                if candidate_id is not None and (
                    caption_candidates[candidate_id].text != expected_text
                ):
                    raise ValueError(
                        f"case {case.case_key} caption text does not match candidate {candidate_id}"
                    )

        clusters = Counter(
            media_rows[case.media_asset_id].perceptual_hash or f"media:{case.media_asset_id}"
            for case in cases
        )
        dominated = {
            cluster: count
            for cluster, count in clusters.items()
            if count > maximum_cases_per_duplicate_cluster
        }
        if dominated:
            raise ValueError(
                "duplicate clusters exceed the preregistered case limit: "
                + json.dumps(dominated, sort_keys=True)
            )

        canonical_cases = [
            case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_key)
        ]
        preregistration = {
            "version": "runway-blind-study-v1",
            "target": target,
            "baseline_identity": baseline_identity.strip(),
            "challenger_identity": challenger_identity.strip(),
            "seed": seed,
            "minimum_export_cases": 50,
            "unique_media_required": True,
            "maximum_cases_per_duplicate_cluster": (maximum_cases_per_duplicate_cluster),
            "primary_outcome": "creator_pairwise_preference",
            "secondary_outcomes": [
                "caption_acceptability",
                "edit_rate",
                "image_verdict",
                "pairing_verdict",
                "decision_time",
            ],
            "origin_hidden": True,
            "final_holdout_excluded_from_training": True,
        }
        study_key = configuration_hash(
            {
                "channel_id": channel_id,
                "preregistration": preregistration,
                "cases": canonical_cases,
            }
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(BlindStudy).where(BlindStudy.study_key == study_key).limit(1)
            )
            if existing is not None:
                return self.status(existing.id) | {"created": False}
            study = BlindStudy(
                channel_id=channel_id,
                study_key=study_key,
                target=target,
                baseline_identity=baseline_identity.strip(),
                challenger_identity=challenger_identity.strip(),
                seed=seed,
                status="planned",
                split_policy_json=json.dumps(
                    {
                        "allowed": [
                            "development",
                            "tuning",
                            "final_holdout",
                        ],
                        "group_protected": True,
                        "final_holdout_trainable": False,
                    },
                    sort_keys=True,
                ),
                preregistration_json=json.dumps(preregistration, sort_keys=True),
                case_count=len(cases),
                response_count=0,
            )
            session.add(study)
            session.flush()
            prepared: list[tuple[str, BlindStudyCasePlan, bool]] = []
            for case in cases:
                baseline_first = (
                    int.from_bytes(
                        hashlib.sha256(f"{seed}:{case.case_key}:order".encode()).digest()[:8],
                        "big",
                    )
                    % 2
                    == 0
                )
                order_hash = hashlib.sha256(f"{seed}:{case.case_key}:display".encode()).hexdigest()
                prepared.append((order_hash, case, baseline_first))
            prepared.sort(key=lambda item: (item[0], item[1].case_key))
            for display_order, (_order_hash, case, baseline_first) in enumerate(
                prepared,
                start=1,
            ):
                first_caption = case.baseline_caption if baseline_first else case.challenger_caption
                second_caption = (
                    case.challenger_caption if baseline_first else case.baseline_caption
                )
                first_candidate_id = (
                    case.baseline_candidate_id if baseline_first else case.challenger_candidate_id
                )
                second_candidate_id = (
                    case.challenger_candidate_id if baseline_first else case.baseline_candidate_id
                )
                hidden = {
                    "first_origin": ("baseline" if baseline_first else "challenger"),
                    "second_origin": ("challenger" if baseline_first else "baseline"),
                    "baseline_candidate_id": case.baseline_candidate_id,
                    "challenger_candidate_id": case.challenger_candidate_id,
                    "baseline_identity": baseline_identity.strip(),
                    "challenger_identity": challenger_identity.strip(),
                }
                session.add(
                    BlindStudyCase(
                        blind_study_id=study.id,
                        case_key=case.case_key,
                        candidate_image_id=case.candidate_image_id,
                        media_asset_id=case.media_asset_id,
                        split=case.split,
                        group_key=case.group_key,
                        first_caption=first_caption,
                        second_caption=second_caption,
                        first_candidate_id=first_candidate_id,
                        second_candidate_id=second_candidate_id,
                        order_token=("baseline_first" if baseline_first else "challenger_first"),
                        hidden_label_json=json.dumps(hidden, sort_keys=True),
                        metadata_json=json.dumps(case.metadata, sort_keys=True),
                        selection_rationale_json=json.dumps(
                            case.selection_rationale,
                            sort_keys=True,
                        ),
                        display_order=display_order,
                    )
                )
            study_id = study.id
        return self.status(study_id) | {"created": True}

    def export(
        self,
        study_id: int,
        output: Path,
        *,
        minimum_cases: int = 50,
    ) -> Path:
        if minimum_cases < 1:
            raise ValueError("minimum_cases must be positive")
        with self.database.session() as session:
            study = self._study_for_channel(session, study_id)
            cases = session.scalars(
                select(BlindStudyCase)
                .where(BlindStudyCase.blind_study_id == study.id)
                .order_by(BlindStudyCase.display_order)
            ).all()
            media = {
                row.id: row
                for row in session.scalars(
                    select(MediaAsset).where(
                        MediaAsset.id.in_([case.media_asset_id for case in cases])
                    )
                )
            }
        if len(cases) < minimum_cases:
            raise ValueError(
                f"study has {len(cases)} cases; at least {minimum_cases} are "
                "required for the preregistered export"
            )
        payload = {
            "format_version": "runway-blind-review-v1",
            "study_id": study.id,
            "study_key": study.study_key,
            "target": study.target,
            "case_count": len(cases),
            "instructions": (
                "Review captions without attempting to infer their origin. "
                "Use only first, second, or tie."
            ),
            "response_schema": {
                "choice": ["first", "second", "tie"],
                "acceptable_choices": ["first", "second"],
                "verdicts": ["accepted", "rejected", "unsure"],
            },
            "cases": [
                {
                    "case_key": case.case_key,
                    "media_asset_id": case.media_asset_id,
                    "media_url": self._media_url(media[case.media_asset_id]),
                    "first_caption": case.first_caption,
                    "second_caption": case.second_caption,
                    "metadata": self._public_metadata(case.metadata_json),
                }
                for case in cases
            ],
            "responses": [],
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        for forbidden in (
            study.baseline_identity,
            study.challenger_identity,
            "first_origin",
            "second_origin",
            "order_token",
            "hidden_label",
        ):
            if forbidden and forbidden in serialized:
                raise RuntimeError("blind export would reveal hidden model provenance")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
        with self.database.session() as session:
            loaded = session.get(BlindStudy, study.id)
            if loaded is not None and loaded.status == "planned":
                loaded.status = "exported"
        return output

    def import_responses(
        self,
        study_id: int,
        responses: list[BlindStudyImportResponse],
        *,
        review_session: str,
        reviewer_label: str = "local-creator",
        reviewer_kind: str = "creator",
    ) -> dict[str, object]:
        if not responses:
            raise ValueError("response import requires at least one response")
        if not review_session.strip() or not reviewer_label.strip():
            raise ValueError("review session and reviewer label are required")
        if reviewer_kind not in {"creator", "engineering_fixture"}:
            raise ValueError("reviewer_kind must be creator or engineering_fixture")
        if reviewer_kind == "engineering_fixture" and (
            self.settings.agent_runtime != "mock" or self.settings.publishing_enabled
        ):
            raise ValueError(
                "engineering fixture reviews require mock runtime and publishing disabled"
            )
        response_keys = [response.case_key for response in responses]
        if len(set(response_keys)) != len(response_keys):
            raise ValueError("duplicate case responses in one import are not allowed")

        created_preferences = 0
        with self.database.session() as session:
            study = self._study_for_channel(session, study_id)
            cases = {
                case.case_key: case
                for case in session.scalars(
                    select(BlindStudyCase).where(BlindStudyCase.blind_study_id == study.id)
                )
            }
            missing = sorted(set(response_keys) - set(cases))
            if missing:
                raise LookupError("responses reference unknown study cases: " + ", ".join(missing))
            case_ids = [cases[key].id for key in response_keys]
            duplicates = list(
                session.scalars(
                    select(BlindStudyResponse.blind_study_case_id).where(
                        BlindStudyResponse.blind_study_case_id.in_(case_ids)
                    )
                )
            )
            if duplicates:
                raise ValueError(
                    "one or more study cases already have a human response; "
                    "duplicate labels were rejected"
                )
            for response in responses:
                case = cases[response.case_key]
                row = BlindStudyResponse(
                    blind_study_case_id=case.id,
                    choice=response.choice,
                    reviewer_kind=reviewer_kind,
                    reviewer_label=reviewer_label.strip(),
                    review_session=review_session.strip(),
                    acceptable_choices_json=json.dumps(sorted(set(response.acceptable_choices))),
                    edited_final_caption=response.edited_final_caption,
                    image_verdict=response.image_verdict,
                    caption_verdict=response.caption_verdict,
                    pairing_verdict=response.pairing_verdict,
                    reason_codes_json=json.dumps(sorted(set(response.reason_codes))),
                    note=response.note,
                    decision_time_ms=response.decision_time_ms,
                    started_at=response.started_at,
                    submitted_at=utcnow(),
                )
                session.add(row)
                session.flush()
                if response.choice != "tie":
                    self._add_study_preference(
                        session,
                        study=study,
                        case=case,
                        response=row,
                        response_payload=response,
                    )
                    created_preferences += 1
            study.response_count = int(
                session.scalar(
                    select(func.count(BlindStudyResponse.id))
                    .join(
                        BlindStudyCase,
                        BlindStudyCase.id == BlindStudyResponse.blind_study_case_id,
                    )
                    .where(BlindStudyCase.blind_study_id == study.id)
                )
                or 0
            )
            if study.response_count == study.case_count:
                study.status = "completed"
                study.completed_at = utcnow()
            elif study.response_count:
                study.status = "in_progress"
        return self.status(study_id) | {
            "imported": len(responses),
            "pairwise_preferences_created": created_preferences,
            "label_source": ("human" if reviewer_kind == "creator" else "engineering_fixture"),
        }

    def status(self, study_id: int) -> dict[str, object]:
        with self.database.session() as session:
            study = self._study_for_channel(session, study_id)
            split_counts = {
                str(split): int(count)
                for split, count in session.execute(
                    select(BlindStudyCase.split, func.count(BlindStudyCase.id))
                    .where(BlindStudyCase.blind_study_id == study.id)
                    .group_by(BlindStudyCase.split)
                )
            }
            response_count = int(
                session.scalar(
                    select(func.count(BlindStudyResponse.id))
                    .join(
                        BlindStudyCase,
                        BlindStudyCase.id == BlindStudyResponse.blind_study_case_id,
                    )
                    .where(BlindStudyCase.blind_study_id == study.id)
                )
                or 0
            )
            human_response_count = int(
                session.scalar(
                    select(func.count(BlindStudyResponse.id))
                    .join(
                        BlindStudyCase,
                        BlindStudyCase.id == BlindStudyResponse.blind_study_case_id,
                    )
                    .where(
                        BlindStudyCase.blind_study_id == study.id,
                        BlindStudyResponse.reviewer_kind == "creator",
                    )
                )
                or 0
            )
            return {
                "study_id": study.id,
                "study_key": study.study_key,
                "target": study.target,
                "status": study.status,
                "case_count": study.case_count,
                "response_count": response_count,
                "human_response_count": human_response_count,
                "pending_count": study.case_count - response_count,
                "split_counts": split_counts,
                "minimum_export_cases": 50,
                "export_ready": study.case_count >= 50,
                "human_results_complete": human_response_count == study.case_count,
            }

    def report(self, study_id: int) -> dict[str, object]:
        with self.database.session() as session:
            study = self._study_for_channel(session, study_id)
            rows = session.execute(
                select(BlindStudyCase, BlindStudyResponse)
                .join(
                    BlindStudyResponse,
                    BlindStudyResponse.blind_study_case_id == BlindStudyCase.id,
                )
                .where(BlindStudyCase.blind_study_id == study.id)
                .where(BlindStudyResponse.reviewer_kind == "creator")
                .order_by(BlindStudyCase.display_order)
            ).all()
        outcomes = Counter[str]()
        edit_count = 0
        for case, response in rows:
            if response.choice == "tie":
                outcomes["tie"] += 1
            else:
                hidden = self._json_object(case.hidden_label_json)
                outcomes[str(hidden[f"{response.choice}_origin"])] += 1
            if response.edited_final_caption:
                chosen = (
                    case.first_caption
                    if response.choice == "first"
                    else (case.second_caption if response.choice == "second" else "")
                )
                if response.edited_final_caption != chosen:
                    edit_count += 1
        reviewed = len(rows)
        return {
            **self.status(study_id),
            "outcomes": {
                "baseline_wins": outcomes["baseline"],
                "challenger_wins": outcomes["challenger"],
                "ties": outcomes["tie"],
            },
            "edit_count": edit_count,
            "sample_size": reviewed,
            "statistical_claim": (
                "pending human responses"
                if reviewed < study.case_count
                else ("descriptive only; confidence analysis is required before activation")
            ),
            "synthetic_labels": 0,
        }

    def _add_study_preference(
        self,
        session: Session,
        *,
        study: BlindStudy,
        case: BlindStudyCase,
        response: BlindStudyResponse,
        response_payload: BlindStudyImportResponse,
    ) -> None:
        preferred_text = case.first_caption if response.choice == "first" else case.second_caption
        dispreferred_text = (
            case.second_caption if response.choice == "first" else case.first_caption
        )
        preferred_id = (
            case.first_candidate_id if response.choice == "first" else case.second_candidate_id
        )
        dispreferred_id = (
            case.second_candidate_id if response.choice == "first" else case.first_candidate_id
        )
        preferred_record = (
            session.get(CaptionCandidateRecord, preferred_id) if preferred_id is not None else None
        )
        dispreferred_record = (
            session.get(CaptionCandidateRecord, dispreferred_id)
            if dispreferred_id is not None
            else None
        )
        context = {
            "study_id": study.id,
            "study_case_id": case.id,
            "study_split": case.split,
            "review_session": response.review_session,
            "reviewer_label": response.reviewer_label,
            "randomization_seed": study.seed,
            "decision_time_ms": response.decision_time_ms,
            "acceptable_choices": response_payload.acceptable_choices,
            "image_verdict": response.image_verdict,
            "caption_verdict": response.caption_verdict,
            "pairing_verdict": response.pairing_verdict,
        }
        preferred_json, preferred_hash = snapshot_json_and_hash(
            preferred_text,
            candidate_components(preferred_record),
            context=context,
        )
        dispreferred_json, dispreferred_hash = snapshot_json_and_hash(
            dispreferred_text,
            candidate_components(dispreferred_record),
            context=context,
        )
        idempotency_key = f"study:{study.id}:case:{case.id}:response"
        session.add(
            PairwisePreference(
                channel_id=study.channel_id,
                proposal_id=None,
                candidate_image_id=case.candidate_image_id,
                preferred_candidate_id=preferred_id,
                preferred_text=preferred_text,
                dispreferred_candidate_id=dispreferred_id,
                dispreferred_text=dispreferred_text,
                preference_source=(
                    "blind_creator_study"
                    if response.reviewer_kind == "creator"
                    else "offline_study_fixture"
                ),
                label_source=(
                    "human" if response.reviewer_kind == "creator" else "engineering_fixture"
                ),
                strength=1.0,
                reason_codes_json=response.reason_codes_json,
                policy_version=None,
                target=study.target,
                source_proposal_event_id=None,
                source_exposure_id=None,
                source_event_key=idempotency_key,
                derivation_version="blind-study-import-v1",
                idempotency_key=idempotency_key,
                preferred_features_json=preferred_json,
                dispreferred_features_json=dispreferred_json,
                context_snapshot_json=json.dumps(context, sort_keys=True),
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                feature_snapshot_hash=configuration_hash(
                    {
                        "preferred": preferred_hash,
                        "dispreferred": dispreferred_hash,
                    }
                ),
                group_key=case.group_key,
                taxonomy_version=TAXONOMY_VERSION,
                verifier_version=VERIFIER_VERSION,
                representation_sets_json="{}",
                learning_split=case.split,
                source_study_response_id=response.id,
            )
        )

    def _study_for_channel(self, session: Session, study_id: int) -> BlindStudy:
        channel = get_channel(session, self.settings.channel_handle)
        study = session.get(BlindStudy, study_id)
        if study is None or study.channel_id != channel.id:
            raise LookupError(f"blind study {study_id} was not found")
        return study

    @staticmethod
    def _channel_media_ids(session: Session, channel_id: int) -> set[int]:
        historical = set(
            session.scalars(
                select(PostMedia.media_asset_id)
                .join(Post, Post.id == PostMedia.post_id)
                .where(Post.channel_id == channel_id)
            )
        )
        candidates = set(
            session.scalars(
                select(CandidateImage.media_asset_id)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
            )
        )
        generated = set(
            session.scalars(
                select(GeneratedAssetLineage.media_asset_id).where(
                    GeneratedAssetLineage.channel_id == channel_id
                )
            )
        )
        return historical | candidates | generated

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        payload: object = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("stored study JSON is malformed")
        return payload

    @classmethod
    def _public_metadata(cls, value: str) -> dict[str, object]:
        metadata = cls._json_object(value)
        blocked = {
            "provider",
            "model",
            "origin",
            "rank",
            "score",
            "configuration",
            "baseline",
            "challenger",
        }
        return {key: item for key, item in metadata.items() if key.casefold() not in blocked}

    @staticmethod
    def _media_url(media: MediaAsset) -> str:
        path = Path(media.local_path)
        try:
            relative = path.relative_to("media")
        except ValueError:
            return f"/api/media/{media.id}"
        return f"/media/{relative.as_posix()}"


class ActiveLearningService:
    """Persist deterministic, uncertainty-first, diversity-aware label queues."""

    strategy_version = "uncertainty-disagreement-diversity-v1"

    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def select(
        self,
        cases: list[ActiveLearningCase],
        *,
        target: RequestedTarget,
        limit: int = 20,
        seed: int = 20260718,
        maximum_per_cluster: int = 1,
    ) -> dict[str, object]:
        if not cases:
            raise ValueError("active-learning selection requires candidate cases")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if maximum_per_cluster < 1:
            raise ValueError("maximum_per_cluster must be positive")
        filtered = [case for case in cases if case.target == target]
        if not filtered:
            raise ValueError(f"no active-learning cases target {target}")
        identities = [(case.entity_type, case.entity_id) for case in filtered]
        if len(set(identities)) != len(identities):
            raise ValueError("active-learning candidate identities must be unique")

        cluster_frequency = Counter(case.diversity_cluster for case in filtered)
        ranked = sorted(
            filtered,
            key=lambda case: (
                -self._base_priority(case, cluster_frequency),
                hashlib.sha256(f"{seed}:{case.entity_type}:{case.entity_id}".encode()).hexdigest(),
            ),
        )
        selected: list[ActiveLearningCase] = []
        cluster_counts: Counter[str] = Counter()
        remaining: list[ActiveLearningCase] = []
        for case in ranked:
            if cluster_counts[case.diversity_cluster] < maximum_per_cluster:
                selected.append(case)
                cluster_counts[case.diversity_cluster] += 1
            else:
                remaining.append(case)
            if len(selected) == min(limit, len(filtered)):
                break
        if len(selected) < min(limit, len(filtered)):
            for case in remaining:
                selected.append(case)
                if len(selected) == min(limit, len(filtered)):
                    break

        configuration = {
            "strategy_version": self.strategy_version,
            "target": target,
            "limit": limit,
            "seed": seed,
            "maximum_per_cluster": maximum_per_cluster,
            "final_holdout_eligible": False,
        }
        selected_payload = [case.model_dump(mode="json") for case in selected]
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            batch_key = configuration_hash(
                {
                    "channel_id": channel.id,
                    "configuration": configuration,
                    "selected": selected_payload,
                }
            )
            existing = session.scalar(
                select(ActiveLearningBatch)
                .where(ActiveLearningBatch.batch_key == batch_key)
                .limit(1)
            )
            if existing is not None:
                return self.status(existing.id) | {"created": False}
            batch = ActiveLearningBatch(
                channel_id=channel.id,
                batch_key=batch_key,
                target=target,
                strategy_version=self.strategy_version,
                seed=seed,
                configuration_json=json.dumps(configuration, sort_keys=True),
                status="selected",
                selection_count=len(selected),
            )
            session.add(batch)
            session.flush()
            for rank, case in enumerate(selected, start=1):
                base = self._base_priority(case, cluster_frequency)
                diversity = 1.0 / cluster_frequency[case.diversity_cluster]
                session.add(
                    ActiveLearningSelection(
                        active_learning_batch_id=batch.id,
                        entity_type=case.entity_type,
                        entity_id=case.entity_id,
                        group_key=case.group_key,
                        split=case.split,
                        priority_rank=rank,
                        uncertainty_score=case.uncertainty_score,
                        diversity_score=diversity,
                        rationale_json=json.dumps(
                            {
                                "target": case.target,
                                "base_priority": base,
                                "disagreement_score": case.disagreement_score,
                                "information_gain_score": (case.information_gain_score),
                                "diversity_cluster": case.diversity_cluster,
                                "cluster_frequency": cluster_frequency[case.diversity_cluster],
                                "reasons": case.reasons,
                                "metadata": case.metadata,
                            },
                            sort_keys=True,
                        ),
                    )
                )
            batch_id = batch.id
        return self.status(batch_id) | {"created": True}

    def select_caption_slates(
        self,
        *,
        limit: int = 20,
        seed: int = 20260718,
    ) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            slates = session.scalars(
                select(CaptionSlate)
                .where(
                    CaptionSlate.channel_id == channel.id,
                    CaptionSlate.status.in_(("completed", "ready")),
                )
                .order_by(CaptionSlate.id)
            ).all()
            candidates = session.scalars(
                select(CaptionCandidateRecord)
                .where(
                    CaptionCandidateRecord.channel_id == channel.id,
                    CaptionCandidateRecord.caption_slate_id.in_([slate.id for slate in slates]),
                    CaptionCandidateRecord.eligible.is_(True),
                )
                .order_by(
                    CaptionCandidateRecord.caption_slate_id,
                    CaptionCandidateRecord.rank,
                )
            ).all()
            candidate_images = {
                row.id: row
                for row in session.scalars(
                    select(CandidateImage).where(
                        CandidateImage.id.in_([slate.candidate_image_id for slate in slates])
                    )
                )
            }
            media = {
                row.id: row
                for row in session.scalars(
                    select(MediaAsset).where(
                        MediaAsset.id.in_(
                            [
                                candidate_images[slate.candidate_image_id].media_asset_id
                                for slate in slates
                                if slate.candidate_image_id in candidate_images
                            ]
                        )
                    )
                )
            }
        by_slate: defaultdict[int, list[CaptionCandidateRecord]] = defaultdict(list)
        for candidate in candidates:
            by_slate[candidate.caption_slate_id].append(candidate)
        cases: list[ActiveLearningCase] = []
        for slate in slates:
            rows = by_slate.get(slate.id, [])
            if len(rows) < 2:
                continue
            scores = sorted((row.final_score for row in rows), reverse=True)
            gap = max(0.0, min(1.0, scores[0] - scores[1]))
            component_winners = {
                max(rows, key=lambda row: getattr(row, field)).id
                for field in (
                    "grounding_score",
                    "style_score",
                    "novelty_score",
                    "pairing_score",
                    "preference_score",
                )
            }
            disagreement = min(1.0, (len(component_winners) - 1) / 4)
            image = candidate_images.get(slate.candidate_image_id)
            stored_media = media.get(image.media_asset_id) if image is not None else None
            cluster = (
                stored_media.perceptual_hash
                if stored_media is not None and stored_media.perceptual_hash
                else f"candidate:{slate.candidate_image_id}"
            )
            cases.append(
                ActiveLearningCase(
                    entity_type="caption_slate",
                    entity_id=slate.id,
                    group_key=f"candidate:{slate.candidate_image_id}",
                    split="development",
                    target="caption",
                    uncertainty_score=1.0 - gap,
                    disagreement_score=disagreement,
                    information_gain_score=min(1.0, len(rows) / 8),
                    diversity_cluster=cluster,
                    reasons=[
                        "close_top_scores" if gap < 0.15 else "ranker_disagreement",
                    ],
                    metadata={
                        "candidate_image_id": slate.candidate_image_id,
                        "candidate_count": len(rows),
                        "top_score_gap": gap,
                    },
                )
            )
        return self.select(cases, target="caption", limit=limit, seed=seed)

    def status(self, batch_id: int) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            batch = session.get(ActiveLearningBatch, batch_id)
            if batch is None or batch.channel_id != channel.id:
                raise LookupError(f"active-learning batch {batch_id} was not found")
            rows = session.scalars(
                select(ActiveLearningSelection)
                .where(ActiveLearningSelection.active_learning_batch_id == batch.id)
                .order_by(ActiveLearningSelection.priority_rank)
            ).all()
            return {
                "batch_id": batch.id,
                "batch_key": batch.batch_key,
                "target": batch.target,
                "strategy_version": batch.strategy_version,
                "status": batch.status,
                "selection_count": batch.selection_count,
                "configuration": json.loads(batch.configuration_json),
                "selections": [
                    {
                        "entity_type": row.entity_type,
                        "entity_id": row.entity_id,
                        "group_key": row.group_key,
                        "split": row.split,
                        "priority_rank": row.priority_rank,
                        "uncertainty_score": row.uncertainty_score,
                        "diversity_score": row.diversity_score,
                        "rationale": json.loads(row.rationale_json),
                        "label_status": row.label_status,
                    }
                    for row in rows
                ],
            }

    def export(self, batch_id: int, output: Path) -> Path:
        payload = self.status(batch_id)
        payload["exported_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return output

    @staticmethod
    def _base_priority(
        case: ActiveLearningCase,
        cluster_frequency: Counter[str],
    ) -> float:
        rarity = 1.0 / cluster_frequency[case.diversity_cluster]
        return (
            0.5 * case.uncertainty_score
            + 0.25 * case.disagreement_score
            + 0.15 * case.information_gain_score
            + 0.10 * rarity
        )
