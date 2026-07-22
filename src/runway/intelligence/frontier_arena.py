from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    BlindStudy,
    BlindStudyCase,
    BlindStudyResponse,
    CandidateImage,
    MediaAsset,
    Post,
    PostMedia,
    SearchRun,
    utcnow,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import configuration_hash

ArenaArm = Literal["raw_frontier", "runway_frontier", "runway_weaker"]


class ArenaArmOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: str = Field(min_length=1, max_length=280)
    caption_candidate_id: int | None = Field(default=None, ge=1)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=80)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    grounding_problems: list[str] = Field(default_factory=list)


class ArenaCasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=1, max_length=128)
    media_asset_id: int = Field(ge=1)
    candidate_image_id: int | None = Field(default=None, ge=1)
    split: Literal["development", "tuning", "locked_holdout"] = "development"
    group_key: str = Field(min_length=1, max_length=128)
    raw_frontier: ArenaArmOutput
    runway_frontier: ArenaArmOutput
    runway_weaker: ArenaArmOutput

    @field_validator("case_key", "group_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return "-".join(value.strip().casefold().split())


class ArenaResponseImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str
    choice: Literal["first", "second", "third", "tie"]
    acceptable_choices: list[Literal["first", "second", "third"]] = Field(default_factory=list)
    edited_final_caption: str | None = Field(default=None, max_length=280)
    image_verdict: str | None = Field(default=None, max_length=40)
    caption_verdict: str | None = Field(default=None, max_length=40)
    pairing_verdict: str | None = Field(default=None, max_length=40)
    reason_codes: list[str] = Field(default_factory=list)
    grounding_problems: list[str] = Field(default_factory=list)
    genericness_score: float | None = Field(default=None, ge=0, le=1)
    repetition_score: float | None = Field(default=None, ge=0, le=1)
    note: str | None = Field(default=None, max_length=2000)
    decision_time_ms: float | None = Field(default=None, ge=0)
    started_at: datetime | None = None

    @field_validator("case_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return "-".join(value.strip().casefold().split())


class FrontierArenaService:
    """Blinded three-arm proof harness; model generation remains provider-neutral."""

    format_version = "runway-frontier-arena-v1"
    arm_names: tuple[ArenaArm, ...] = (
        "raw_frontier",
        "runway_frontier",
        "runway_weaker",
    )

    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def plan(
        self,
        *,
        study_key: str,
        cases: list[ArenaCasePlan],
        seed: int = 20260722,
        target_case_count: int = 50,
    ) -> dict[str, object]:
        normalized_key = "-".join(study_key.strip().casefold().split())
        if not normalized_key:
            raise ValueError("arena study_key is required")
        if not cases:
            raise ValueError("arena planning requires at least one case")
        if len({case.case_key for case in cases}) != len(cases):
            raise ValueError("arena case keys must be unique")
        if len({case.media_asset_id for case in cases}) != len(cases):
            raise ValueError("arena cases must use unique media")
        if target_case_count < 1 or target_case_count > 1000:
            raise ValueError("target_case_count must be between 1 and 1000")
        for case in cases:
            if (
                case.raw_frontier.provider != case.runway_frontier.provider
                or case.raw_frontier.model != case.runway_frontier.model
            ):
                raise ValueError(
                    "raw-frontier and Runway-frontier arms must use the same base model"
                )
        identities = {
            arm: {
                (
                    getattr(case, arm).provider,
                    getattr(case, arm).model,
                    getattr(case, arm).prompt_version,
                )
                for case in cases
            }
            for arm in self.arm_names
        }
        inconsistent = [arm for arm, values in identities.items() if len(values) != 1]
        if inconsistent:
            raise ValueError(
                "arena arm identities must remain fixed across every case: "
                + ", ".join(inconsistent)
            )

        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            existing = session.scalar(
                select(BlindStudy).where(BlindStudy.study_key == normalized_key).limit(1)
            )
            if existing is not None:
                return self.status(existing.id)
            historical_media = (
                select(PostMedia.media_asset_id)
                .join(
                    Post,
                    Post.id == PostMedia.post_id,
                )
                .where(Post.channel_id == channel.id)
            )
            candidate_media = (
                select(CandidateImage.media_asset_id)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel.id)
            )
            allowed_media = set(session.scalars(historical_media).all()) | set(
                session.scalars(candidate_media).all()
            )
            media_ids = {
                case.media_asset_id for case in cases if case.media_asset_id in allowed_media
            }
            if media_ids != {case.media_asset_id for case in cases}:
                raise LookupError("one or more arena media assets do not exist")
            candidate_ids = {
                case.candidate_image_id for case in cases if case.candidate_image_id is not None
            }
            if candidate_ids:
                existing_candidates = set(
                    session.scalars(
                        select(CandidateImage.id)
                        .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                        .where(
                            CandidateImage.id.in_(candidate_ids),
                            SearchRun.channel_id == channel.id,
                        )
                    ).all()
                )
                if existing_candidates != candidate_ids:
                    raise LookupError("one or more arena candidate images do not exist")

            persisted_identities = {
                arm: {
                    "provider": getattr(cases[0], arm).provider,
                    "model": getattr(cases[0], arm).model,
                    "prompt_version": getattr(cases[0], arm).prompt_version,
                }
                for arm in self.arm_names
            }
            preregistration = {
                "format_version": self.format_version,
                "directional_target_cases": target_case_count,
                "mature_target_cases": 150,
                "primary_outcome": "human creator blind preference",
                "secondary_outcomes": [
                    "no_edit_acceptance",
                    "pairing_verdict",
                    "decision_time_ms",
                    "grounding_problems",
                    "genericness",
                    "repetition",
                    "cost_usd",
                    "latency_ms",
                ],
                "superiority_threshold": 0.60,
                "human_labels_only": True,
                "synthetic_labels": 0,
            }
            study = BlindStudy(
                channel_id=channel.id,
                study_key=normalized_key,
                target="frontier_arena",
                baseline_identity="raw_frontier",
                challenger_identity="runway_frontier+runway_weaker",
                seed=seed,
                status="planned",
                split_policy_json=json.dumps(
                    {"group_protected": True, "holdout_is_locked": True}, sort_keys=True
                ),
                preregistration_json=json.dumps(preregistration, sort_keys=True),
                arm_identities_json=json.dumps(persisted_identities, sort_keys=True),
                case_count=len(cases),
                response_count=0,
            )
            session.add(study)
            session.flush()
            rng = random.Random(seed)
            for display_order, case in enumerate(cases, start=1):
                arms = list(self.arm_names)
                rng.shuffle(arms)
                outputs = {arm: getattr(case, arm) for arm in self.arm_names}
                hidden = {
                    "first_origin": arms[0],
                    "second_origin": arms[1],
                    "third_origin": arms[2],
                    "label_source": "hidden_system_provenance",
                }
                metrics = {
                    arm: {
                        "cost_usd": outputs[arm].cost_usd,
                        "latency_ms": outputs[arm].latency_ms,
                        "grounding_problems": outputs[arm].grounding_problems,
                    }
                    for arm in self.arm_names
                }
                session.add(
                    BlindStudyCase(
                        blind_study_id=study.id,
                        case_key=case.case_key,
                        candidate_image_id=case.candidate_image_id,
                        media_asset_id=case.media_asset_id,
                        split=case.split,
                        group_key=case.group_key,
                        first_caption=outputs[arms[0]].caption,
                        second_caption=outputs[arms[1]].caption,
                        third_caption=outputs[arms[2]].caption,
                        first_candidate_id=outputs[arms[0]].caption_candidate_id,
                        second_candidate_id=outputs[arms[1]].caption_candidate_id,
                        third_candidate_id=outputs[arms[2]].caption_candidate_id,
                        order_token=hashlib.sha256(
                            f"{seed}:{case.case_key}:{','.join(arms)}".encode()
                        ).hexdigest()[:24],
                        hidden_label_json=json.dumps(hidden, sort_keys=True),
                        metadata_json=json.dumps(
                            {
                                "format_version": self.format_version,
                                "caption_hashes": {
                                    position: configuration_hash(caption)
                                    for position, caption in {
                                        "first": outputs[arms[0]].caption,
                                        "second": outputs[arms[1]].caption,
                                        "third": outputs[arms[2]].caption,
                                    }.items()
                                },
                            },
                            sort_keys=True,
                        ),
                        selection_rationale_json=json.dumps(
                            {"same_image_all_arms": True, "base_model_parity_a_b": True},
                            sort_keys=True,
                        ),
                        arm_metrics_json=json.dumps(metrics, sort_keys=True),
                        display_order=display_order,
                    )
                )
            study_id = study.id
        return self.status(study_id)

    def export(self, study_id: int, destination: Path) -> Path:
        with self.database.session() as session:
            study = self._study(session, study_id)
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
                ).all()
            }
        payload = {
            "format_version": self.format_version,
            "study_id": study.id,
            "study_key": study.study_key,
            "instructions": (
                "Review captions without guessing their source. Choose first, second, third, "
                "or tie and complete the structured verdict fields."
            ),
            "cases": [
                {
                    "case_key": case.case_key,
                    "display_order": case.display_order,
                    "image_url": self._media_url(media[case.media_asset_id]),
                    "first_caption": case.first_caption,
                    "second_caption": case.second_caption,
                    "third_caption": case.third_caption,
                    "acceptable_choices": ["first", "second", "third"],
                }
                for case in cases
            ],
            "responses": [],
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        for forbidden in ("raw_frontier", "runway_frontier", "runway_weaker", "hidden_label"):
            if forbidden in encoded:
                raise RuntimeError("arena export would reveal hidden model provenance")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
        with self.database.session() as session:
            persisted = self._study(session, study_id)
            persisted.status = "in_review"
        return destination

    def import_responses(
        self,
        study_id: int,
        *,
        responses: list[ArenaResponseImport],
        review_session: str,
        reviewer_label: str = "local-creator",
        reviewer_kind: str = "creator",
    ) -> dict[str, object]:
        if not responses:
            raise ValueError("arena response import requires responses")
        if not review_session.strip() or not reviewer_label.strip():
            raise ValueError("review_session and reviewer_label are required")
        if reviewer_kind not in {"creator", "engineering_fixture"}:
            raise ValueError("reviewer_kind must be creator or engineering_fixture")
        if reviewer_kind == "engineering_fixture" and (
            self.settings.agent_runtime != "mock" or self.settings.publishing_enabled
        ):
            raise ValueError(
                "engineering fixture reviews require mock runtime and publishing disabled"
            )
        if len({row.case_key for row in responses}) != len(responses):
            raise ValueError("duplicate arena case responses are not allowed")
        with self.database.session() as session:
            study = self._study(session, study_id)
            cases = {
                row.case_key: row
                for row in session.scalars(
                    select(BlindStudyCase).where(BlindStudyCase.blind_study_id == study.id)
                ).all()
            }
            missing = sorted({row.case_key for row in responses} - set(cases))
            if missing:
                raise LookupError("responses reference unknown arena cases: " + ", ".join(missing))
            case_ids = [cases[row.case_key].id for row in responses]
            existing = session.scalar(
                select(func.count(BlindStudyResponse.id)).where(
                    BlindStudyResponse.blind_study_case_id.in_(case_ids)
                )
            )
            if existing:
                raise ValueError("one or more arena cases already have a human response")
            for response in responses:
                case = cases[response.case_key]
                session.add(
                    BlindStudyResponse(
                        blind_study_case_id=case.id,
                        choice=response.choice,
                        reviewer_kind=reviewer_kind,
                        reviewer_label=reviewer_label.strip(),
                        review_session=review_session.strip(),
                        acceptable_choices_json=json.dumps(
                            sorted(set(response.acceptable_choices))
                        ),
                        edited_final_caption=response.edited_final_caption,
                        image_verdict=response.image_verdict,
                        caption_verdict=response.caption_verdict,
                        pairing_verdict=response.pairing_verdict,
                        reason_codes_json=json.dumps(sorted(set(response.reason_codes))),
                        grounding_problems_json=json.dumps(
                            sorted(set(response.grounding_problems))
                        ),
                        genericness_score=response.genericness_score,
                        repetition_score=response.repetition_score,
                        note=response.note,
                        decision_time_ms=response.decision_time_ms,
                        started_at=response.started_at,
                        model_cost_json="{}",
                        latency_json="{}",
                    )
                )
            session.flush()
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
            study.status = "completed" if study.response_count == study.case_count else "in_review"
            if study.status == "completed":
                study.completed_at = utcnow()
        return {
            "study_id": study_id,
            "imported": len(responses),
            "label_source": ("human" if reviewer_kind == "creator" else "engineering_fixture"),
            "synthetic_labels": 0,
        }

    def status(self, study_id: int) -> dict[str, object]:
        with self.database.session() as session:
            study = self._study(session, study_id)
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
                "status": study.status,
                "case_count": study.case_count,
                "response_count": response_count,
                "human_response_count": human_response_count,
                "pending_count": study.case_count - response_count,
                "directional_target_met": human_response_count >= 50,
                "mature_target_met": human_response_count >= 150,
                "label_source": "human_only",
            }

    def report(self, study_id: int) -> dict[str, object]:
        with self.database.session() as session:
            study = self._study(session, study_id)
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
        wins: Counter[str] = Counter()
        ties = 0
        edits = 0
        decision_times: list[float] = []
        costs: dict[str, float] = {arm: 0.0 for arm in self.arm_names}
        latencies: dict[str, list[float]] = {arm: [] for arm in self.arm_names}
        for case, response in rows:
            hidden = json.loads(case.hidden_label_json)
            metrics = json.loads(case.arm_metrics_json)
            if response.choice == "tie":
                ties += 1
            else:
                wins[str(hidden[f"{response.choice}_origin"])] += 1
            captions = {
                "first": case.first_caption,
                "second": case.second_caption,
                "third": case.third_caption or "",
            }
            if response.edited_final_caption and response.choice != "tie":
                edits += response.edited_final_caption.strip() != captions[response.choice].strip()
            if response.decision_time_ms is not None:
                decision_times.append(response.decision_time_ms)
            for arm in self.arm_names:
                arm_metrics = metrics.get(arm, {}) if isinstance(metrics, dict) else {}
                cost = arm_metrics.get("cost_usd") if isinstance(arm_metrics, dict) else None
                latency = arm_metrics.get("latency_ms") if isinstance(arm_metrics, dict) else None
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    costs[arm] += float(cost)
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    latencies[arm].append(float(latency))
        decisive = sum(wins.values())
        runway_frontier_rate = wins["runway_frontier"] / decisive if decisive else 0.0
        runway_weaker_rate = wins["runway_weaker"] / decisive if decisive else 0.0
        frontier_interval = self._wilson(wins["runway_frontier"], decisive)
        weaker_interval = self._wilson(wins["runway_weaker"], decisive)
        enough = len(rows) >= 50
        return {
            "study_id": study.id,
            "human_responses": len(rows),
            "wins": dict(wins),
            "ties": ties,
            "no_edit_acceptance_rate": (
                round((len(rows) - edits) / len(rows), 6) if rows else None
            ),
            "mean_decision_time_ms": (
                round(sum(decision_times) / len(decision_times), 3) if decision_times else None
            ),
            "cost_usd_by_arm": {key: round(value, 6) for key, value in costs.items()},
            "mean_latency_ms_by_arm": {
                arm: (round(sum(values) / len(values), 3) if values else None)
                for arm, values in latencies.items()
            },
            "runway_frontier_win_rate": round(runway_frontier_rate, 6),
            "runway_frontier_wilson_95": frontier_interval,
            "runway_weaker_win_rate": round(runway_weaker_rate, 6),
            "runway_weaker_wilson_95": weaker_interval,
            "supports_runway_frontier_beats_raw": bool(
                enough and runway_frontier_rate > 0.5 and frontier_interval[0] > 0.5
            ),
            "supports_runway_weaker_beats_raw": bool(
                enough and runway_weaker_rate > 0.5 and weaker_interval[0] > 0.5
            ),
            "superiority_claim_blocked_reason": (
                None if enough else "fewer than 50 genuine human responses"
            ),
            "synthetic_labels": 0,
        }

    @staticmethod
    def _wilson(successes: int, total: int) -> list[float]:
        if total <= 0:
            return [0.0, 1.0]
        z = 1.959963984540054
        proportion = successes / total
        denominator = 1 + z * z / total
        centre = (proportion + z * z / (2 * total)) / denominator
        margin = (
            z
            * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
            / denominator
        )
        return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]

    @staticmethod
    def _media_url(media: MediaAsset) -> str:
        raw = Path(media.local_path).as_posix()
        try:
            relative = Path(raw).relative_to("media").as_posix()
        except ValueError:
            relative = raw
        return f"/media/{relative}"

    @staticmethod
    def _study(session: Session, study_id: int) -> BlindStudy:
        study = session.get(BlindStudy, study_id)
        if study is None or study.target != "frontier_arena":
            raise LookupError(f"frontier arena {study_id} was not found")
        return study
