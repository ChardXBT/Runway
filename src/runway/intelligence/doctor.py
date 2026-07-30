from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from sqlalchemy import func, select, text

from runway.analysis.service import AnalysisService
from runway.captions.feature_snapshots import FEATURE_SCHEMA_VERSION
from runway.captions.feedback import ALLOWED_REASON_CODES
from runway.captions.preference_models import PRODUCT_CHALLENGER_MINIMUM_LABELS
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ActiveLearningBatch,
    AnnotationCorrection,
    AnnotationRefreshRun,
    BlindStudy,
    CandidateExposure,
    CaptionCandidateRecord,
    CaptionFeedback,
    CaptionSlate,
    ComposedRetrievalExample,
    FeedbackSignal,
    GeneratedAssetLineage,
    ImageGenerationRun,
    IntelligenceActivation,
    IntelligenceAgentRun,
    IntelligenceAgentStep,
    IntelligenceExperiment,
    IntelligenceRetrievalRun,
    MediaAsset,
    ModelRun,
    MultimodalRerankRun,
    PairwisePreference,
    Post,
    PostAnnotation,
    PostMedia,
    PreferenceModelVersion,
    Proposal,
    RepresentationRecord,
    RepresentationSet,
    RepresentationSetItem,
    RetrievalEvidenceRecord,
)
from runway.db.repositories import get_channel
from runway.db.schema_contract import load_schema_snapshot, schema_snapshot
from runway.intelligence.agent_harness import IntelligenceCapabilityRegistry
from runway.intelligence.embeddings import (
    RepresentationStore,
    configuration_hash,
    content_hash,
)

Severity = Literal["critical", "warning", "information"]


def _json_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is not an integer")
    return value


@dataclass(frozen=True)
class DoctorFinding:
    severity: Severity
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class DoctorReport:
    database_path: str
    channel_handle: str
    started_at: str
    findings: list[DoctorFinding] = field(default_factory=list)
    measurements: dict[str, object] = field(default_factory=dict)

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        **details: object,
    ) -> None:
        self.findings.append(
            DoctorFinding(
                severity=severity,
                code=code,
                message=message,
                details=details,
            )
        )

    @property
    def critical_count(self) -> int:
        return sum(item.severity == "critical" for item in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(item.severity == "warning" for item in self.findings)

    def as_dict(self) -> dict[str, object]:
        counts = Counter(item.severity for item in self.findings)
        return {
            "status": "failed" if self.critical_count else "passed",
            "database_path": self.database_path,
            "channel_handle": self.channel_handle,
            "started_at": self.started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "counts": {
                "critical": counts["critical"],
                "warning": counts["warning"],
                "information": counts["information"],
            },
            "measurements": self.measurements,
            "findings": [item.as_dict() for item in self.findings],
        }

    def human_text(self) -> str:
        status = "FAILED" if self.critical_count else "PASSED"
        lines = [
            f"Runway intelligence doctor: {status}",
            f"Database: {self.database_path}",
            (
                f"Findings: {self.critical_count} critical, "
                f"{self.warning_count} warning, "
                f"{sum(item.severity == 'information' for item in self.findings)} "
                "information"
            ),
        ]
        for item in self.findings:
            lines.append(f"[{item.severity.upper()}] {item.code}: {item.message}")
            if item.details:
                lines.append("  " + json.dumps(item.details, sort_keys=True, default=str))
        return "\n".join(lines)


class IntelligenceDoctor:
    """Read-only integrity audit for the canonical intelligence flywheel."""

    forbidden_capability_tokens = IntelligenceCapabilityRegistry.FORBIDDEN_TOKENS

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        verify_media_files: bool = True,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.verify_media_files = verify_media_files
        self.report = DoctorReport(
            database_path=str(self.settings.database_path),
            channel_handle=self.settings.channel_handle,
            started_at=datetime.now(UTC).isoformat(),
        )

    def run(self) -> DoctorReport:
        checks: tuple[tuple[str, Callable[[], None]], ...] = (
            ("core", self._check_core),
            ("channel_isolation", self._check_channel_isolation),
            ("representations", self._check_representations),
            ("annotations", self._check_annotations),
            ("retrieval", self._check_retrieval),
            ("captions", self._check_captions),
            ("learning", self._check_learning),
            ("preference_models", self._check_preference_models),
            ("neural_intelligence", self._check_neural_intelligence),
            ("feedback", self._check_feedback),
            ("agent_harness", self._check_agent_harness),
            ("image_generation", self._check_image_generation),
            ("experiments", self._check_experiments),
        )
        for name, check in checks:
            try:
                check()
            except Exception as exc:
                self.report.add(
                    "critical",
                    f"doctor.{name}.internal_error",
                    "The doctor could not complete this audit section.",
                    error=f"{type(exc).__name__}: {exc}",
                )
        if not self.report.findings:
            self.report.add(
                "information",
                "doctor.clean",
                "No intelligence integrity issues were detected.",
            )
        return self.report

    def _check_core(self) -> None:
        path = self.settings.database_path
        wal_path = Path(f"{path}-wal")
        snapshot_path = (
            self.settings.project_root / "docs" / "schema" / "intelligence-data-flywheel.json"
        )
        expected = load_schema_snapshot(snapshot_path)
        expected_migration = expected.get("migration")
        self.report.measurements.update(
            {
                "database_bytes": path.stat().st_size if path.exists() else 0,
                "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
                "publishing_enabled": self.settings.publishing_enabled,
            }
        )
        if self.settings.publishing_enabled:
            self.report.add(
                "warning",
                "core.publishing_enabled",
                "Publishing is enabled while running a database intelligence audit.",
            )
        with sqlite3.connect(path) as connection:
            integrity_rows = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            migration_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if integrity_rows != ["ok"]:
            self.report.add(
                "critical",
                "core.sqlite_integrity",
                "SQLite integrity_check failed.",
                results=integrity_rows[:20],
            )
        else:
            self.report.add(
                "information",
                "core.sqlite_integrity",
                "SQLite integrity_check returned ok.",
            )
        if foreign_keys:
            self.report.add(
                "critical",
                "core.foreign_keys",
                "Foreign-key violations were found.",
                count=len(foreign_keys),
                examples=[list(row) for row in foreign_keys[:20]],
            )
        observed_migration = str(migration_row[0]) if migration_row else None
        self.report.measurements["migration"] = observed_migration
        if observed_migration != expected_migration:
            self.report.add(
                "critical",
                "core.migration_head",
                "Database migration is not at the canonical head.",
                expected=expected_migration,
                observed=observed_migration,
            )

        observed = schema_snapshot(self.database.engine)
        self.report.measurements["schema_fingerprint"] = observed["fingerprint"]
        if observed["fingerprint"] != expected.get("fingerprint"):
            self.report.add(
                "critical",
                "core.schema_fingerprint",
                "Database schema does not match the committed contract.",
                expected=expected.get("fingerprint"),
                observed=observed["fingerprint"],
            )

    def _check_channel_isolation(self) -> None:
        checks = {
            "representation_set_item": """
                SELECT COUNT(*)
                FROM representation_set_items i
                JOIN representation_sets s ON s.id = i.representation_set_id
                WHERE i.channel_id != s.channel_id
            """,
            "retrieval_evidence": """
                SELECT COUNT(*)
                FROM retrieval_evidence_records e
                JOIN intelligence_retrieval_runs r ON r.id = e.retrieval_run_id
                WHERE e.channel_id != r.channel_id
            """,
            "caption_slate_candidate": """
                SELECT COUNT(*)
                FROM caption_slates s
                JOIN candidate_images c ON c.id = s.candidate_image_id
                JOIN search_runs r ON r.id = c.search_run_id
                WHERE s.channel_id != r.channel_id
            """,
            "caption_exposure": """
                SELECT COUNT(*)
                FROM caption_exposures e
                JOIN proposals p ON p.id = e.proposal_id
                JOIN caption_slates s ON s.id = e.caption_slate_id
                WHERE e.channel_id != p.channel_id OR e.channel_id != s.channel_id
            """,
            "preference_proposal": """
                SELECT COUNT(*)
                FROM pairwise_preferences p
                JOIN proposals q ON q.id = p.proposal_id
                WHERE p.proposal_id IS NOT NULL AND p.channel_id != q.channel_id
            """,
            "preference_candidate": """
                SELECT COUNT(*)
                FROM pairwise_preferences p
                JOIN candidate_images c ON c.id = p.candidate_image_id
                JOIN search_runs r ON r.id = c.search_run_id
                WHERE p.candidate_image_id IS NOT NULL AND p.channel_id != r.channel_id
            """,
            "preference_caption_candidate": """
                SELECT COUNT(*)
                FROM pairwise_preferences p
                JOIN caption_candidate_records c
                  ON c.id = p.preferred_candidate_id
                     OR c.id = p.dispreferred_candidate_id
                WHERE p.channel_id != c.channel_id
            """,
            "feedback_proposal": """
                SELECT COUNT(*)
                FROM feedback_signals f
                JOIN proposals p ON p.id = f.proposal_id
                WHERE f.proposal_id IS NOT NULL AND f.channel_id != p.channel_id
            """,
            "preference_model_dataset": """
                SELECT COUNT(*)
                FROM preference_model_versions m
                JOIN preference_datasets d ON d.dataset_id = m.dataset_id
                WHERE m.channel_id != d.channel_id
            """,
            "generation_lineage": """
                SELECT COUNT(*)
                FROM generated_asset_lineage l
                JOIN image_generation_runs r ON r.id = l.generation_run_id
                WHERE l.channel_id != r.channel_id
            """,
            "study_response_preference": """
                SELECT COUNT(*)
                FROM pairwise_preferences p
                JOIN blind_study_responses r ON r.id = p.source_study_response_id
                JOIN blind_study_cases c ON c.id = r.blind_study_case_id
                JOIN blind_studies s ON s.id = c.blind_study_id
                WHERE p.source_study_response_id IS NOT NULL
                  AND p.channel_id != s.channel_id
            """,
            "candidate_exposure": """
                SELECT COUNT(*)
                FROM candidate_exposures e
                JOIN candidate_images c ON c.id = e.candidate_image_id
                JOIN search_runs r ON r.id = c.search_run_id
                WHERE e.channel_id != r.channel_id
            """,
            "multimodal_rerank": """
                SELECT COUNT(*)
                FROM multimodal_rerank_runs m
                JOIN candidate_images c ON c.id = m.candidate_image_id
                JOIN search_runs r ON r.id = c.search_run_id
                LEFT JOIN caption_slates s ON s.id = m.caption_slate_id
                WHERE m.channel_id != r.channel_id
                   OR (m.caption_slate_id IS NOT NULL AND m.channel_id != s.channel_id)
            """,
        }
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            self.report.measurements["channel_id"] = channel.id
            for name, sql in checks.items():
                count = int(session.scalar(text(sql)) or 0)
                if count:
                    self.report.add(
                        "critical",
                        f"channel_isolation.{name}",
                        "Cross-channel intelligence linkage was detected.",
                        count=count,
                    )

    def _check_representations(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            active = session.scalars(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == channel.id,
                    RepresentationSet.active.is_(True),
                )
                .order_by(RepresentationSet.id)
            ).all()
            all_sets = session.scalars(
                select(RepresentationSet)
                .where(RepresentationSet.channel_id == channel.id)
                .order_by(RepresentationSet.id)
            ).all()
            records = session.scalars(
                select(RepresentationRecord)
                .where(RepresentationRecord.channel_id == channel.id)
                .order_by(RepresentationRecord.id)
            ).all()
            eligible_posts = int(
                session.scalar(
                    select(func.count(Post.id)).where(
                        Post.channel_id == channel.id,
                        Post.is_training_eligible.is_(True),
                    )
                )
                or 0
            )
        grouped: defaultdict[tuple[str, str], list[RepresentationSet]] = defaultdict(list)
        for row in active:
            grouped[(row.scope, row.purpose)].append(row)
        for (scope, purpose), rows in grouped.items():
            if len(rows) > 1:
                self.report.add(
                    "critical",
                    "representations.conflicting_active_sets",
                    "More than one representation set is active for one resolver key.",
                    scope=scope,
                    purpose=purpose,
                    set_ids=[row.id for row in rows],
                )
        expected_scopes = {
            "historical_text": "historical_caption_semantics",
            "historical_image": "historical_visual_semantics",
            "historical_multimodal": "historical_pair_semantics",
        }
        if eligible_posts:
            for scope, purpose in expected_scopes.items():
                if (scope, purpose) not in grouped:
                    self.report.add(
                        "warning",
                        "representations.missing_active_set",
                        "No active historical representation set exists.",
                        scope=scope,
                        purpose=purpose,
                    )
        all_set_record_ids: set[int] = set()
        active_set_record_ids: set[int] = set()
        for representation_set in all_sets:
            with self.database.session() as session:
                items = session.scalars(
                    select(RepresentationSetItem)
                    .where(RepresentationSetItem.representation_set_id == representation_set.id)
                    .order_by(RepresentationSetItem.id)
                ).all()
            record_ids = {
                item.representation_record_id
                for item in items
                if item.representation_record_id is not None
            }
            all_set_record_ids.update(record_ids)
            if representation_set.active:
                active_set_record_ids.update(record_ids)
            complete = sum(
                item.status == "complete" and item.representation_record_id is not None
                for item in items
            )
            failed = sum(item.status == "failed" for item in items)
            stale = sum(item.status == "stale" for item in items)
            partial = (
                len(items) != representation_set.expected_count
                or complete != representation_set.expected_count
                or failed
                or stale
            )
            if representation_set.active and (representation_set.status != "active" or partial):
                self.report.add(
                    "critical",
                    "representations.partial_active_set",
                    "An active representation set is incomplete or invalid.",
                    set_id=representation_set.id,
                    status=representation_set.status,
                    expected=representation_set.expected_count,
                    item_count=len(items),
                    complete=complete,
                    failed=failed,
                    stale=stale,
                )
            elif failed:
                self.report.add(
                    "warning",
                    "representations.failed_backfill",
                    "An inactive representation set has failed records.",
                    set_id=representation_set.id,
                    failed=failed,
                )
            self._check_representation_items(representation_set, items)

        invalid_active_vectors: list[dict[str, object]] = []
        invalid_inactive_vectors: list[dict[str, object]] = []
        for record in records:
            try:
                vectors = RepresentationStore.vectors(record)
                if not np.all(np.isfinite(vectors)):
                    raise ValueError("nonfinite values")
                if record.vector_count != vectors.shape[0]:
                    raise ValueError("incorrect vector count")
                if record.normalized:
                    norms = np.linalg.norm(vectors, axis=1)
                    if np.any(np.abs(norms - 1.0) > 0.01):
                        raise ValueError("normalized vector has invalid norm")
            except ValueError as exc:
                finding = {"record_id": record.id, "error": str(exc)}
                if record.id in active_set_record_ids or record.id not in all_set_record_ids:
                    invalid_active_vectors.append(finding)
                else:
                    invalid_inactive_vectors.append(finding)
        if invalid_active_vectors:
            self.report.add(
                "critical",
                "representations.invalid_vectors",
                "Invalid active or read-through representation vectors were found.",
                count=len(invalid_active_vectors),
                examples=invalid_active_vectors[:20],
            )
        if invalid_inactive_vectors:
            self.report.add(
                "information",
                "representations.preserved_invalid_inactive_vectors",
                "Invalid vectors are quarantined in preserved inactive sets.",
                count=len(invalid_inactive_vectors),
                examples=invalid_inactive_vectors[:20],
                active_read_impact=False,
                remediation="none; preserve immutable superseded evidence",
            )
        with self.database.session() as session:
            orphan_records = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM representation_records r
                        LEFT JOIN representation_set_items i
                          ON i.representation_record_id = r.id
                        WHERE r.channel_id = :channel_id AND i.id IS NULL
                        """
                    ),
                    {"channel_id": channel.id},
                )
                or 0
            )
        if orphan_records:
            self.report.add(
                "information",
                "representations.read_through_records",
                "Read-through representation records exist outside immutable sets.",
                count=orphan_records,
            )
        self.report.measurements["representation_records"] = len(records)
        self.report.measurements["active_representation_sets"] = len(active)

    def _check_representation_items(
        self,
        representation_set: RepresentationSet,
        items: Sequence[RepresentationSetItem],
    ) -> None:
        examples: list[dict[str, object]] = []
        seen_media: set[int] = set()
        with self.database.session() as session:
            for item in items:
                record = (
                    session.get(RepresentationRecord, item.representation_record_id)
                    if item.representation_record_id is not None
                    else None
                )
                if record is not None and (
                    record.channel_id != representation_set.channel_id
                    or item.channel_id != representation_set.channel_id
                    or record.entity_type != item.entity_type
                    or record.entity_id != item.entity_id
                    or record.field != item.field
                    or record.purpose != representation_set.purpose
                    or record.provider != representation_set.provider
                    or record.model != representation_set.model
                    or record.model_version != representation_set.model_version
                    or record.configuration_hash != representation_set.configuration_hash
                    or record.source_content_hash != item.source_content_hash
                ):
                    examples.append({"item_id": item.id, "reason": "identity mismatch"})
                    continue
                expected_hash: str | None = None
                if item.entity_type == "post" and item.field == "caption":
                    post = session.get(Post, item.entity_id)
                    expected_hash = content_hash(post.caption or "") if post else None
                elif item.entity_type == "media_asset" and item.field == "image":
                    media = session.get(MediaAsset, item.entity_id)
                    expected_hash = media.sha256 if media else None
                    if media is not None and self.verify_media_files and media.id not in seen_media:
                        seen_media.add(media.id)
                        file_error = self._verify_media_file(media)
                        if file_error is not None:
                            examples.append({"item_id": item.id, "reason": file_error})
                elif item.entity_type == "post" and item.field == "image_caption":
                    post = session.get(Post, item.entity_id)
                    media = session.scalar(
                        select(MediaAsset)
                        .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                        .where(PostMedia.post_id == item.entity_id)
                        .order_by(PostMedia.position, MediaAsset.id)
                        .limit(1)
                    )
                    if post is not None and media is not None:
                        expected_hash = configuration_hash(
                            {
                                "media_sha256": media.sha256,
                                "text": post.caption or "",
                            }
                        )
                if expected_hash is None:
                    examples.append({"item_id": item.id, "reason": "source entity missing"})
                elif expected_hash != item.source_content_hash:
                    examples.append({"item_id": item.id, "reason": "stale source hash"})
        if examples:
            severity: Severity = "critical" if representation_set.active else "warning"
            self.report.add(
                severity,
                "representations.stale_or_mismatched_items",
                "Representation-set items do not match canonical sources.",
                set_id=representation_set.id,
                count=len(examples),
                examples=examples[:20],
            )

    def _verify_media_file(self, media: MediaAsset) -> str | None:
        raw = Path(media.local_path)
        path = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if path != root and root not in path.parents:
            return "media path escapes data root"
        if not path.is_file():
            return "media file is missing"
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != media.sha256:
            return "media file hash does not match catalogue"
        return None

    def _check_annotations(self) -> None:
        compatible = AnalysisService.compatible_annotation_versions
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            eligible = list(
                session.scalars(
                    select(Post.id).where(
                        Post.channel_id == channel.id,
                        Post.is_training_eligible.is_(True),
                    )
                )
            )
            annotated = set(
                session.scalars(
                    select(PostAnnotation.post_id).where(
                        PostAnnotation.post_id.in_(eligible),
                        PostAnnotation.annotation_version.in_(compatible),
                    )
                )
            )
            versions = list(
                session.scalars(
                    select(PostAnnotation.annotation_version)
                    .join(Post, Post.id == PostAnnotation.post_id)
                    .where(Post.channel_id == channel.id)
                    .distinct()
                )
            )
            failed_refreshes = session.scalars(
                select(AnnotationRefreshRun).where(
                    AnnotationRefreshRun.channel_id == channel.id,
                    AnnotationRefreshRun.status.in_(("failed", "partial")),
                )
            ).all()
            corrections = session.execute(
                select(AnnotationCorrection, PostAnnotation)
                .join(
                    PostAnnotation,
                    PostAnnotation.id == AnnotationCorrection.post_annotation_id,
                )
                .join(Post, Post.id == PostAnnotation.post_id)
                .where(Post.channel_id == channel.id)
            ).all()
            annotations = session.scalars(
                select(PostAnnotation)
                .join(Post, Post.id == PostAnnotation.post_id)
                .where(Post.channel_id == channel.id)
            ).all()
        missing = sorted(set(eligible) - annotated)
        if missing:
            self.report.add(
                "warning",
                "annotations.missing",
                "Training-eligible posts lack a compatible annotation.",
                count=len(missing),
                post_ids=missing[:20],
            )
        if len(versions) > 1:
            self.report.add(
                "information",
                "annotations.mixed_versions",
                "Multiple immutable annotation versions are preserved.",
                versions=sorted(versions),
            )
        if failed_refreshes:
            self.report.add(
                "warning",
                "annotations.failed_refresh",
                "Annotation refresh runs have failures.",
                run_ids=[row.id for row in failed_refreshes],
            )
        malformed_corrections: list[int] = []
        for correction, annotation in corrections:
            try:
                payload = self._json_object(correction.fields_json)
            except ValueError:
                malformed_corrections.append(correction.id)
                continue
            if annotation.annotation_version not in compatible or not payload:
                malformed_corrections.append(correction.id)
        if malformed_corrections:
            self.report.add(
                "warning",
                "annotations.incompatible_corrections",
                "Correction overlays are empty, malformed, or target incompatible versions.",
                correction_ids=malformed_corrections[:20],
            )
        missing_confidence: list[int] = []
        for annotation in annotations:
            try:
                confidence = self._json_object(annotation.model_confidence_json)
            except ValueError:
                missing_confidence.append(annotation.id)
                continue
            if not confidence:
                missing_confidence.append(annotation.id)
        if missing_confidence:
            self.report.add(
                "warning",
                "annotations.missing_confidence",
                "Annotations lack persisted confidence metadata.",
                count=len(missing_confidence),
                annotation_ids=missing_confidence[:20],
            )
        self.report.measurements["annotation_coverage"] = {
            "eligible": len(eligible),
            "compatible": len(annotated),
            "missing": len(missing),
        }

    def _check_retrieval(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            runs = session.scalars(
                select(IntelligenceRetrievalRun)
                .where(IntelligenceRetrievalRun.channel_id == channel.id)
                .order_by(IntelligenceRetrievalRun.id)
            ).all()
            evidence_counts = {
                int(run_id): int(count)
                for run_id, count in session.execute(
                    select(
                        RetrievalEvidenceRecord.retrieval_run_id,
                        func.count(RetrievalEvidenceRecord.id),
                    )
                    .join(
                        IntelligenceRetrievalRun,
                        IntelligenceRetrievalRun.id == RetrievalEvidenceRecord.retrieval_run_id,
                    )
                    .where(IntelligenceRetrievalRun.channel_id == channel.id)
                    .group_by(RetrievalEvidenceRecord.retrieval_run_id)
                )
            }
            selected_counts = {
                int(run_id): int(count)
                for run_id, count in session.execute(
                    select(
                        RetrievalEvidenceRecord.retrieval_run_id,
                        func.count(RetrievalEvidenceRecord.id),
                    )
                    .join(
                        IntelligenceRetrievalRun,
                        IntelligenceRetrievalRun.id == RetrievalEvidenceRecord.retrieval_run_id,
                    )
                    .where(
                        IntelligenceRetrievalRun.channel_id == channel.id,
                        RetrievalEvidenceRecord.retrieval_channel == "fusion",
                        RetrievalEvidenceRecord.selected.is_(True),
                    )
                    .group_by(RetrievalEvidenceRecord.retrieval_run_id)
                )
            }
            duplicate_ranks = session.execute(
                text(
                    """
                    SELECT retrieval_run_id, selected_rank, COUNT(*)
                    FROM retrieval_evidence_records
                    WHERE selected = 1 AND retrieval_channel = 'fusion'
                    GROUP BY retrieval_run_id, selected_rank
                    HAVING COUNT(*) > 1
                    """
                )
            ).all()
        bad_completed = [
            run.id for run in runs if run.status == "completed" and not evidence_counts.get(run.id)
        ]
        if bad_completed:
            self.report.add(
                "critical",
                "retrieval.completed_without_evidence",
                "Completed retrieval runs have no evidence records.",
                run_ids=bad_completed[:20],
            )
        empty_selected = [
            run.id for run in runs if run.status == "completed" and not selected_counts.get(run.id)
        ]
        if empty_selected:
            self.report.add(
                "critical",
                "retrieval.completed_without_selection",
                "Completed retrieval runs have no selected fusion evidence.",
                run_ids=empty_selected[:20],
            )
        if duplicate_ranks:
            self.report.add(
                "critical",
                "retrieval.duplicate_selected_rank",
                "Selected retrieval evidence contains duplicate ranks.",
                examples=[list(row) for row in duplicate_ranks[:20]],
            )
        malformed: list[int] = []
        missing_provenance: list[int] = []
        for run in runs:
            try:
                configuration = self._json_object(run.retrieval_configuration_json)
                sets = self._json_object(run.representation_sets_json)
                diagnostics = self._json_object(run.cache_diagnostics_json)
            except ValueError:
                malformed.append(run.id)
                continue
            if (
                configuration.get("version") == "hybrid-retrieval-2"
                and run.status == "completed"
                and (not isinstance(sets, dict) or not isinstance(diagnostics, dict))
            ):
                missing_provenance.append(run.id)
        if malformed or missing_provenance:
            self.report.add(
                "critical",
                "retrieval.malformed_provenance",
                "Retrieval run provenance is missing or malformed.",
                run_ids=sorted(set(malformed + missing_provenance))[:20],
            )
        self.report.measurements["retrieval_runs"] = len(runs)

    def _check_captions(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            slates = session.scalars(
                select(CaptionSlate).where(CaptionSlate.channel_id == channel.id)
            ).all()
            candidate_counts = {
                int(slate_id): int(count)
                for slate_id, count in session.execute(
                    select(
                        CaptionCandidateRecord.caption_slate_id,
                        func.count(CaptionCandidateRecord.id),
                    )
                    .where(CaptionCandidateRecord.channel_id == channel.id)
                    .group_by(CaptionCandidateRecord.caption_slate_id)
                )
            }
            candidates = session.scalars(
                select(CaptionCandidateRecord).where(
                    CaptionCandidateRecord.channel_id == channel.id
                )
            ).all()
            displayed_without_exposure = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM caption_candidate_records c
                        LEFT JOIN caption_exposures e
                          ON e.caption_slate_id = c.caption_slate_id
                        WHERE c.channel_id = :channel_id
                          AND c.displayed = 1
                          AND e.id IS NULL
                        """
                    ),
                    {"channel_id": channel.id},
                )
                or 0
            )
            invalid_proposals = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM proposals p
                        LEFT JOIN caption_slates s ON s.id = p.caption_slate_id
                        WHERE p.channel_id = :channel_id
                          AND p.caption_slate_id IS NOT NULL
                          AND (
                            s.id IS NULL
                            OR s.channel_id != p.channel_id
                            OR s.candidate_image_id != p.candidate_image_id
                          )
                        """
                    ),
                    {"channel_id": channel.id},
                )
                or 0
            )
        missing_candidates = [
            slate.id
            for slate in slates
            if slate.status in {"completed", "ready"} and not candidate_counts.get(slate.id)
        ]
        if missing_candidates:
            self.report.add(
                "critical",
                "captions.slate_without_candidates",
                "Ready caption slates contain no candidate records.",
                slate_ids=missing_candidates[:20],
            )
        if displayed_without_exposure:
            self.report.add(
                "warning",
                "captions.displayed_without_exposure",
                "Displayed candidates have no persisted exposure.",
                count=displayed_without_exposure,
            )
        if invalid_proposals:
            self.report.add(
                "critical",
                "captions.invalid_proposal_slate",
                "Proposals reference a missing or mismatched caption slate.",
                count=invalid_proposals,
            )
        legacy_missing: list[int] = []
        human_edit_invalid: list[int] = []
        for candidate in candidates:
            if candidate.origin == "human_edit" and (
                candidate.parent_candidate_id is None
                or not candidate.feature_schema_version
                or not candidate.feature_snapshot_hash
                or not candidate.verifier_result_json
            ):
                human_edit_invalid.append(candidate.id)
            if candidate.origin != "human_edit" and (
                not candidate.feature_schema_version
                or candidate.feature_snapshot_json in {"", "{}"}
                or candidate.verifier_result_json in {"", "{}"}
            ):
                legacy_missing.append(candidate.id)
        if human_edit_invalid:
            self.report.add(
                "critical",
                "captions.invalid_human_edit_candidate",
                "Human edits are not preserved as fully linked, verified candidates.",
                candidate_ids=human_edit_invalid[:20],
            )
        if legacy_missing:
            self.report.add(
                "warning",
                "captions.legacy_candidate_metadata",
                "Legacy caption candidates lack current verifier or feature snapshots.",
                count=len(legacy_missing),
                candidate_ids=legacy_missing[:20],
            )

    def _check_learning(self) -> None:
        allowed_sources = {
            "human",
            "teacher",
            "synthetic",
            "policy",
            "automated",
            "audience",
            "engineering_fixture",
        }
        allowed_targets = {"caption", "image", "pairing"}
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            rows = session.scalars(
                select(PairwisePreference).where(PairwisePreference.channel_id == channel.id)
            ).all()
            duplicate_derivations = session.execute(
                text(
                    """
                    SELECT source_event_key, target, preferred_text,
                           dispreferred_text, COUNT(*)
                    FROM pairwise_preferences
                    WHERE channel_id = :channel_id
                      AND source_event_key IS NOT NULL
                    GROUP BY source_event_key, target, preferred_text,
                             dispreferred_text
                    HAVING COUNT(*) > 1
                    """
                ),
                {"channel_id": channel.id},
            ).all()
            holdout_leakage = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM preference_dataset_items i
                        JOIN pairwise_preferences p
                          ON p.id = i.pairwise_preference_id
                        JOIN preference_datasets d
                          ON d.dataset_id = i.dataset_id
                        WHERE d.channel_id = :channel_id
                          AND p.learning_split = 'final_holdout'
                        """
                    ),
                    {"channel_id": channel.id},
                )
                or 0
            )
            split_leakage = session.execute(
                text(
                    """
                    SELECT dataset_id, group_key, COUNT(DISTINCT split)
                    FROM preference_dataset_items
                    GROUP BY dataset_id, group_key
                    HAVING COUNT(DISTINCT split) > 1
                    """
                )
            ).all()
        if duplicate_derivations:
            self.report.add(
                "critical",
                "learning.duplicate_derivation",
                "Equivalent labels were derived more than once from one event.",
                examples=[list(row) for row in duplicate_derivations[:20]],
            )
        invalid: defaultdict[str, list[int]] = defaultdict(list)
        for row in rows:
            is_study = row.source_study_response_id is not None
            if row.label_source not in allowed_sources:
                invalid["label_source"].append(row.id)
            if row.target not in allowed_targets:
                invalid["target"].append(row.id)
            if row.learning_split not in {
                "development",
                "tuning",
                "final_holdout",
            }:
                invalid["learning_split"].append(row.id)
            if not row.idempotency_key or not row.source_event_key:
                invalid["idempotency"].append(row.id)
            if not row.feature_schema_version or not row.feature_snapshot_hash:
                invalid["feature_snapshot"].append(row.id)
            if (
                not is_study
                and row.label_source == "human"
                and row.preference_source != "legacy"
                and (row.source_proposal_event_id is None or row.source_exposure_id is None)
            ):
                invalid["decision_provenance"].append(row.id)
            if (
                is_study
                and row.label_source == "human"
                and row.preference_source != "blind_creator_study"
            ):
                invalid["study_provenance"].append(row.id)
            if (
                is_study
                and row.label_source == "engineering_fixture"
                and row.preference_source != "offline_study_fixture"
            ):
                invalid["study_fixture_provenance"].append(row.id)
        for name, ids in invalid.items():
            self.report.add(
                "critical",
                f"learning.{name}",
                "Pairwise learning evidence violates its immutable provenance contract.",
                preference_ids=ids[:20],
                count=len(ids),
            )
        if holdout_leakage or split_leakage:
            self.report.add(
                "critical",
                "learning.split_leakage",
                "Final holdout or related groups leaked into a training dataset.",
                final_holdout_items=holdout_leakage,
                group_examples=[list(row) for row in split_leakage[:20]],
            )
        self.report.measurements["pairwise_preferences"] = len(rows)

    def _check_preference_models(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            models = session.scalars(
                select(PreferenceModelVersion).where(
                    PreferenceModelVersion.channel_id == channel.id
                )
            ).all()
            label_counts = {
                str(target): int(count)
                for target, count in session.execute(
                    select(
                        PairwisePreference.target,
                        func.count(PairwisePreference.id),
                    )
                    .where(
                        PairwisePreference.channel_id == channel.id,
                        PairwisePreference.label_source == "human",
                        PairwisePreference.learning_split.in_(("development", "tuning")),
                    )
                    .group_by(PairwisePreference.target)
                )
            }
            dataset_channel_mismatch = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM preference_dataset_items i
                        JOIN preference_datasets d ON d.dataset_id = i.dataset_id
                        JOIN pairwise_preferences p
                          ON p.id = i.pairwise_preference_id
                        WHERE d.channel_id != p.channel_id
                        """
                    )
                )
                or 0
            )
        active_by_target: defaultdict[str, list[PreferenceModelVersion]] = defaultdict(list)
        for model in models:
            if model.active:
                active_by_target[model.target].append(model)
        for target, rows in active_by_target.items():
            if len(rows) > 1:
                self.report.add(
                    "critical",
                    "preference_models.conflicting_active",
                    "Multiple active preference models exist for one target.",
                    target=target,
                    model_ids=[row.id for row in rows],
                )
        for target, count in label_counts.items():
            threshold = PRODUCT_CHALLENGER_MINIMUM_LABELS.get(target, 100)
            if count >= threshold and not active_by_target.get(target):
                self.report.add(
                    "warning",
                    "preference_models.no_active_model",
                    "The product challenger label threshold is met but no model is active.",
                    target=target,
                    labels=count,
                    threshold=threshold,
                )
        invalid: defaultdict[str, list[int]] = defaultdict(list)
        for model in models:
            if model.active and (
                model.status != "active" or model.label_count < model.minimum_label_count
            ):
                invalid["activation_state"].append(model.id)
            if model.feature_schema_version != FEATURE_SCHEMA_VERSION:
                invalid["feature_schema"].append(model.id)
            if not model.dataset_id or not model.configuration_hash:
                invalid["dataset_identity"].append(model.id)
            try:
                parameters = self._json_object(model.parameters_json)
                metrics = self._json_object(model.metrics_json)
                calibration = self._json_object(model.calibration_json)
                weights = parameters.get("weights", [])
                if model.status in {"trained", "active", "superseded"} and (
                    not isinstance(weights, list)
                    or not weights
                    or not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                        for value in weights
                    )
                ):
                    invalid["parameters"].append(model.id)
                if calibration.get("calibrated") is True and (
                    not calibration.get("method")
                    or _json_integer(
                        calibration.get("held_out_pair_count", 0),
                        label="held-out pair count",
                    )
                    < 30
                ):
                    invalid["calibration_claim"].append(model.id)
                if model.status in {"trained", "active", "superseded"}:
                    observed_hash = configuration_hash(
                        {
                            "dataset_id": model.dataset_id,
                            "configuration_hash": model.configuration_hash,
                            "parameters": parameters,
                            "metrics": metrics,
                            "calibration": calibration,
                        }
                    )
                    if observed_hash != model.artifact_hash:
                        invalid["artifact_hash"].append(model.id)
            except (ValueError, TypeError):
                invalid["artifact_json"].append(model.id)
        for name, ids in invalid.items():
            severity: Severity = (
                "critical"
                if any(model.active and model.id in ids for model in models)
                or name in {"artifact_hash", "calibration_claim"}
                else "warning"
            )
            self.report.add(
                severity,
                f"preference_models.{name}",
                "Preference-model artifact integrity validation failed.",
                model_ids=ids[:20],
                count=len(ids),
            )
        if dataset_channel_mismatch:
            self.report.add(
                "critical",
                "preference_models.wrong_channel_training",
                "Preference datasets contain labels from another channel.",
                count=dataset_channel_mismatch,
            )

    def _check_neural_intelligence(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            slates = session.scalars(
                select(CaptionSlate).where(CaptionSlate.channel_id == channel.id)
            ).all()
            model_runs = {
                row.id: row
                for row in session.scalars(
                    select(ModelRun).where(
                        ModelRun.id.in_(
                            [row.model_run_id for row in slates if row.model_run_id is not None]
                        )
                    )
                ).all()
            }
            exposures = session.scalars(
                select(CandidateExposure).where(CandidateExposure.channel_id == channel.id)
            ).all()
            rerank_runs = session.scalars(
                select(MultimodalRerankRun).where(MultimodalRerankRun.channel_id == channel.id)
            ).all()
            composed = session.scalars(
                select(ComposedRetrievalExample).where(
                    ComposedRetrievalExample.channel_id == channel.id
                )
            ).all()

        invalid_provenance: list[int] = []
        false_citations: list[int] = []
        legacy_without_split_provenance = 0
        for slate in slates:
            model_run = model_runs.get(slate.model_run_id) if slate.model_run_id else None
            is_current = model_run is not None and model_run.prompt_version in {
                "captions-v5",
                "captions-v6",
            }
            if not is_current and slate.model_supplied_evidence_json in {"", "[]"}:
                legacy_without_split_provenance += 1
                continue
            try:
                self._json_list(slate.retrieval_selected_evidence_json)
                supplied = self._json_object(slate.model_supplied_evidence_json)
                cited = self._json_object(slate.model_cited_evidence_json)
                self._json_object(slate.ranker_used_evidence_json)
                self._json_object(slate.reranker_run_json)
            except ValueError:
                invalid_provenance.append(slate.id)
                continue
            for evidence_field in ("historical_post_ids", "feedback_signal_ids"):
                supplied_values = supplied.get(evidence_field, [])
                cited_values = cited.get(evidence_field, [])
                if not isinstance(supplied_values, list) or not isinstance(cited_values, list):
                    invalid_provenance.append(slate.id)
                    break
                if not all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in [*supplied_values, *cited_values]
                ):
                    invalid_provenance.append(slate.id)
                    break
                if not set(cited_values).issubset(supplied_values):
                    false_citations.append(slate.id)

        invalid_exposures = [
            row.id
            for row in exposures
            if not 0.0 <= row.final_display_probability <= 1.0
            or row.eligible_pool_size < 1
            or (row.display_position is not None and row.display_position < 1)
        ]
        invalid_rerank_labels = [
            row.id
            for row in rerank_runs
            if row.label_source not in {"human", "teacher", "synthetic", "policy"}
        ]
        false_human_composed = [
            row.id for row in composed if row.label_source == "human" and not row.reviewed
        ]
        findings = {
            "malformed_provenance": invalid_provenance,
            "false_model_citations": false_citations,
            "invalid_exposure_propensity": invalid_exposures,
            "invalid_reranker_label_source": invalid_rerank_labels,
            "unreviewed_human_composed_example": false_human_composed,
        }
        for name, ids in findings.items():
            if ids:
                self.report.add(
                    "critical",
                    f"neural_intelligence.{name}",
                    "Neural-intelligence provenance or label-source integrity failed.",
                    ids=ids[:20],
                    count=len(ids),
                )
        self.report.measurements["neural_intelligence"] = {
            "caption_slates": len(slates),
            "candidate_exposures": len(exposures),
            "multimodal_rerank_runs": len(rerank_runs),
            "composed_retrieval_examples": len(composed),
            "legacy_slates_without_split_provenance": legacy_without_split_provenance,
        }

    def _check_feedback(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            legacy = session.scalars(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(Proposal.channel_id == channel.id)
            ).all()
            signals = session.scalars(
                select(FeedbackSignal).where(FeedbackSignal.channel_id == channel.id)
            ).all()
            normalized_legacy_ids = {
                value
                for value in session.scalars(
                    select(FeedbackSignal.source_caption_feedback_id).where(
                        FeedbackSignal.channel_id == channel.id,
                        FeedbackSignal.source_caption_feedback_id.is_not(None),
                    )
                )
                if value is not None
            }
            duplicates = session.execute(
                text(
                    """
                    SELECT source_caption_feedback_id, target, COUNT(*)
                    FROM feedback_signals
                    WHERE channel_id = :channel_id
                      AND source_caption_feedback_id IS NOT NULL
                    GROUP BY source_caption_feedback_id, target
                    HAVING COUNT(*) > 1
                    """
                ),
                {"channel_id": channel.id},
            ).all()
        missing = [row.id for row in legacy if row.id not in normalized_legacy_ids]
        if missing:
            self.report.add(
                "warning",
                "feedback.legacy_not_reconciled",
                "Legacy feedback has no canonical normalized signal.",
                feedback_ids=missing[:20],
                count=len(missing),
            )
        if duplicates:
            self.report.add(
                "critical",
                "feedback.duplicate_normalized",
                "Legacy feedback was normalized more than once per target.",
                examples=[list(row) for row in duplicates[:20]],
            )
        invalid_targets = [
            row.id for row in signals if row.target not in {"caption", "image", "pairing"}
        ]
        unmapped_reasons: dict[int, list[str]] = {}
        for row in signals:
            try:
                reasons = self._json_list(row.reason_codes_json)
            except ValueError:
                unmapped_reasons[row.id] = ["<malformed-json>"]
                continue
            if not all(isinstance(value, str) for value in reasons):
                unmapped_reasons[row.id] = ["<non-string-reason>"]
                continue
            unexpected = sorted({str(value) for value in reasons} - ALLOWED_REASON_CODES)
            if unexpected:
                unmapped_reasons[row.id] = unexpected
        if invalid_targets:
            self.report.add(
                "critical",
                "feedback.invalid_target",
                "Feedback targets are not separated into canonical domains.",
                signal_ids=invalid_targets[:20],
            )
        if unmapped_reasons:
            self.report.add(
                "warning",
                "feedback.unmapped_reason",
                "Feedback contains unmapped reason codes.",
                examples=dict(list(unmapped_reasons.items())[:20]),
            )
        self.report.measurements["feedback"] = {
            "legacy": len(legacy),
            "normalized": len(signals),
            "legacy_missing_normalized": len(missing),
        }

    def _check_agent_harness(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            runs = session.scalars(
                select(IntelligenceAgentRun).where(IntelligenceAgentRun.channel_id == channel.id)
            ).all()
            steps = session.scalars(
                select(IntelligenceAgentStep)
                .join(
                    IntelligenceAgentRun,
                    IntelligenceAgentRun.id == IntelligenceAgentStep.agent_run_id,
                )
                .where(IntelligenceAgentRun.channel_id == channel.id)
            ).all()
            duplicate_steps = session.execute(
                text(
                    """
                    SELECT agent_run_id, sequence, attempt, COUNT(*)
                    FROM intelligence_agent_steps
                    GROUP BY agent_run_id, sequence, attempt
                    HAVING COUNT(*) > 1
                    """
                )
            ).all()
        if duplicate_steps:
            self.report.add(
                "critical",
                "agent_harness.duplicate_sequence",
                "Agent steps have duplicate sequence/attempt identities.",
                examples=[list(row) for row in duplicate_steps[:20]],
            )
        invalid: defaultdict[str, list[int]] = defaultdict(list)
        for run in runs:
            terminal = run.status in {"completed", "failed", "abstained"}
            if terminal != (run.completed_at is not None):
                invalid["terminal_state"].append(run.id)
            normalized_capability = run.capability.casefold()
            if any(token in normalized_capability for token in self.forbidden_capability_tokens):
                invalid["publishing_capability"].append(run.id)
            try:
                budget = self._json_object(run.budget_json)
                usage = self._json_object(run.usage_json)
                max_tokens = _json_integer(
                    budget.get("max_total_tokens", 0),
                    label="maximum token budget",
                )
                total_tokens = _json_integer(
                    usage.get("total_tokens", 0),
                    label="token usage",
                )
                if max_tokens >= 0 and total_tokens > max_tokens and run.status == "completed":
                    invalid["budget_exceeded"].append(run.id)
                output = self._json_object(run.output_json)
                if output.get("fallback_used") is True:
                    invalid["hidden_fallback"].append(run.id)
            except (ValueError, TypeError):
                invalid["malformed_json"].append(run.id)
        run_by_id = {run.id: run for run in runs}
        registry = IntelligenceCapabilityRegistry()
        for step in steps:
            parent_run = run_by_id.get(step.agent_run_id)
            if parent_run is not None:
                if parent_run.capability == "editorial_pipeline":
                    try:
                        registry.resolve(step.capability)
                    except LookupError:
                        invalid["step_capability"].append(step.id)
                elif step.capability != parent_run.capability:
                    invalid["step_capability"].append(step.id)
            if step.status in {"completed", "failed", "abstained"} and (step.completed_at is None):
                invalid["step_terminal_state"].append(step.id)
        for name, ids in invalid.items():
            self.report.add(
                "critical",
                f"agent_harness.{name}",
                "Persisted intelligence-agent execution violates its safe contract.",
                ids=ids[:20],
                count=len(ids),
            )
        self.report.measurements["agent_runs"] = len(runs)

    def _check_image_generation(self) -> None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            runs = session.scalars(
                select(ImageGenerationRun).where(ImageGenerationRun.channel_id == channel.id)
            ).all()
            lineages = session.scalars(
                select(GeneratedAssetLineage).where(GeneratedAssetLineage.channel_id == channel.id)
            ).all()
        lineage_by_run = Counter(row.generation_run_id for row in lineages)
        missing = [
            run.id for run in runs if run.status == "completed" and not lineage_by_run[run.id]
        ]
        if missing:
            self.report.add(
                "critical",
                "image_generation.missing_lineage",
                "Completed image-generation runs have no output lineage.",
                run_ids=missing[:20],
            )
        invalid: defaultdict[str, list[int]] = defaultdict(list)
        for run in runs:
            try:
                rights = self._json_list(run.reference_rights_json)
                if run.status == "completed":
                    for value in rights:
                        if isinstance(value, dict):
                            decision = value.get("decision")
                            if (
                                not isinstance(decision, dict)
                                or decision.get("outcome") != "allowed"
                            ):
                                invalid["ineligible_reference"].append(run.id)
            except ValueError:
                invalid["reference_rights_json"].append(run.id)
        for lineage in lineages:
            try:
                safety = self._json_object(lineage.safety_result_json)
                lineage_rights = self._json_object(lineage.rights_result_json)
                if not safety:
                    invalid["missing_safety"].append(lineage.id)
                if not lineage_rights:
                    invalid["missing_rights"].append(lineage.id)
            except ValueError:
                invalid["lineage_json"].append(lineage.id)
            if lineage.candidate_image_id is None:
                invalid["legacy_candidate_link"].append(lineage.id)
            if lineage.review_status not in {
                "pending",
                "accepted",
                "rejected",
            }:
                invalid["review_status"].append(lineage.id)
        for name, ids in invalid.items():
            severity: Severity = "warning" if name == "legacy_candidate_link" else "critical"
            self.report.add(
                severity,
                f"image_generation.{name}",
                "Generated-image lineage or safeguard state is incomplete.",
                ids=ids[:20],
                count=len(ids),
            )

    def _check_experiments(self) -> None:
        with self.database.session() as session:
            experiments = session.scalars(
                select(IntelligenceExperiment).order_by(IntelligenceExperiment.experiment_id)
            ).all()
            activations = session.scalars(
                select(IntelligenceActivation).order_by(IntelligenceActivation.id)
            ).all()
            studies = session.scalars(select(BlindStudy)).all()
            active_batches = session.scalars(select(ActiveLearningBatch)).all()
        identities: defaultdict[tuple[int | None, str], list[str]] = defaultdict(list)
        for experiment in experiments:
            identities[(experiment.channel_id, experiment.configuration_hash)].append(
                experiment.experiment_id
            )
        duplicates = {
            f"{channel_id}:{config}": ids
            for (channel_id, config), ids in identities.items()
            if len(ids) > 1
        }
        if duplicates:
            self.report.add(
                "warning",
                "experiments.duplicate_configuration",
                "Multiple experiments share one configuration identity.",
                duplicates=dict(list(duplicates.items())[:20]),
            )
        missing_metrics: list[str] = []
        missing_artifacts: dict[str, list[str]] = {}
        for experiment in experiments:
            try:
                metrics = self._json_object(experiment.metrics_json)
                artifacts = self._json_list(experiment.artifacts_json)
            except ValueError:
                missing_metrics.append(experiment.experiment_id)
                continue
            if experiment.completed_at is not None and not metrics:
                missing_metrics.append(experiment.experiment_id)
            absent: list[str] = []
            for artifact in artifacts:
                if isinstance(artifact, str):
                    path = Path(artifact)
                    if not path.is_absolute():
                        path = self.settings.project_root / path
                    if not path.exists():
                        absent.append(str(path))
            if absent:
                missing_artifacts[experiment.experiment_id] = absent
        if missing_metrics:
            self.report.add(
                "warning",
                "experiments.missing_metrics",
                "Completed experiments lack persisted metrics.",
                experiment_ids=missing_metrics[:20],
            )
        if missing_artifacts:
            self.report.add(
                "warning",
                "experiments.missing_artifacts",
                "Experiment artifact paths are missing.",
                examples=dict(list(missing_artifacts.items())[:20]),
            )
        activation_without_gates = [
            row.id
            for row in activations
            if not self._safe_nonempty_object(row.gate_results_json) and row.action == "activate"
        ]
        if activation_without_gates:
            self.report.add(
                "warning",
                "experiments.activation_without_evidence",
                "Activations lack a non-empty persisted gate result.",
                activation_ids=activation_without_gates[:20],
            )
        self.report.measurements["evaluation_assets"] = {
            "experiments": len(experiments),
            "activations": len(activations),
            "blind_studies": len(studies),
            "active_learning_batches": len(active_batches),
        }

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        payload: object = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    @staticmethod
    def _json_list(value: str) -> list[object]:
        payload: object = json.loads(value)
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
        return payload

    @classmethod
    def _safe_nonempty_object(cls, value: str) -> bool:
        try:
            return bool(cls._json_object(value))
        except ValueError:
            return False
