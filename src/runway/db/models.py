from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from runway.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Channel(Base, TimestampMixin):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    handle: Mapped[str] = mapped_column(String(200), unique=True)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    default_post_time: Mapped[str] = mapped_column(String(5))
    planning_horizon_days: Mapped[int] = mapped_column(Integer)
    duplicate_window_days: Mapped[int] = mapped_column(Integer)


class CaptureRun(Base):
    __tablename__ = "capture_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    mode: Mapped[str] = mapped_column(String(40), index=True)
    channel_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posts_seen: Mapped[int] = mapped_column(Integer, default=0)
    posts_created: Mapped[int] = mapped_column(Integer, default=0)
    posts_updated: Mapped[int] = mapped_column(Integer, default=0)
    media_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    last_cursor_json: Mapped[str] = mapped_column(Text, default="{}")
    selector_adapter_version: Mapped[str] = mapped_column(String(80), default="youtube-v1")
    error_summary: Mapped[str | None] = mapped_column(Text)


class RawPostRecord(Base):
    __tablename__ = "raw_post_records"
    __table_args__ = (UniqueConstraint("capture_run_id", "record_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    capture_run_id: Mapped[int] = mapped_column(ForeignKey("capture_runs.id"), index=True)
    record_key: Mapped[str] = mapped_column(String(128))
    external_post_id: Mapped[str | None] = mapped_column(String(200), index=True)
    permalink: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text)
    raw_dom_snapshot_path: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("channel_id", "external_post_id", name="uq_post_channel_external"),
        UniqueConstraint("permalink", name="uq_post_permalink"),
        Index("ix_posts_channel_published", "channel_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    external_post_id: Mapped[str | None] = mapped_column(String(200))
    permalink: Mapped[str | None] = mapped_column(Text)
    post_type: Mapped[str] = mapped_column(String(40), index=True)
    caption: Mapped[str | None] = mapped_column(Text)
    displayed_date_text: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str] = mapped_column(String(20), index=True)
    like_count: Mapped[int | None] = mapped_column(Integer)
    comment_count: Mapped[int | None] = mapped_column(Integer)
    raw_engagement_json: Mapped[str | None] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    is_training_eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_capture_run_id: Mapped[int] = mapped_column(ForeignKey("capture_runs.id"))


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    local_path: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text, index=True)
    source_page_url: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(100))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    perceptual_hash: Mapped[str] = mapped_column(String(64), index=True)
    crop_resistant_hash: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    embedding_vector: Mapped[bytes | None] = mapped_column(LargeBinary)
    blur_score: Mapped[float | None] = mapped_column(Float)
    quality_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rights_status: Mapped[str] = mapped_column(String(40), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PostMedia(Base):
    __tablename__ = "post_media"
    __table_args__ = (UniqueConstraint("post_id", "position"),)

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    media_asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class PostAnnotation(Base, TimestampMixin):
    __tablename__ = "post_annotations"
    __table_args__ = (UniqueConstraint("post_id", "annotation_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    annotation_version: Mapped[str] = mapped_column(String(80))
    franchise: Mapped[str | None] = mapped_column(String(200))
    show_name: Mapped[str | None] = mapped_column(String(200))
    characters_json: Mapped[str] = mapped_column(Text, default="[]")
    visible_character_count: Mapped[int] = mapped_column(Integer, default=0)
    scene_description: Mapped[str] = mapped_column(Text, default="")
    visual_format: Mapped[str] = mapped_column(String(100), default="unknown")
    composition: Mapped[str] = mapped_column(String(200), default="unknown")
    emotion: Mapped[str] = mapped_column(String(200), default="unknown")
    reaction_potential: Mapped[str] = mapped_column(String(200), default="unknown")
    caption_intent: Mapped[str] = mapped_column(String(100), default="unknown")
    caption_structure: Mapped[str] = mapped_column(String(100), default="unknown")
    humor_style: Mapped[str] = mapped_column(String(100), default="unknown")
    tone: Mapped[str] = mapped_column(String(100), default="unknown")
    text_in_image: Mapped[bool] = mapped_column(Boolean, default=False)
    model_confidence_json: Mapped[str] = mapped_column(Text, default="{}")
    original_output_json: Mapped[str] = mapped_column(Text, default="{}")
    review_status: Mapped[str] = mapped_column(String(40), default="unreviewed")
    reviewed_fields_json: Mapped[str] = mapped_column(Text, default="{}")


class AnnotationCorrection(Base):
    __tablename__ = "annotation_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_annotation_id: Mapped[int] = mapped_column(ForeignKey("post_annotations.id"), index=True)
    fields_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StyleProfile(Base):
    __tablename__ = "style_profiles"
    __table_args__ = (UniqueConstraint("channel_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    catalogue_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile_json: Mapped[str] = mapped_column(Text)
    representative_post_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    excluded_post_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class SimilarityEdge(Base):
    __tablename__ = "similarity_edges"

    source_post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    target_post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    visual_similarity: Mapped[float] = mapped_column(Float)
    caption_similarity: Mapped[float] = mapped_column(Float)
    concept_similarity: Mapped[float] = mapped_column(Float)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    style_profile_id: Mapped[int | None] = mapped_column(ForeignKey("style_profiles.id"))
    query_plan_json: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)


class CandidateImage(Base):
    __tablename__ = "candidate_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id"), index=True)
    media_asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), index=True)
    preview_asset_id: Mapped[int | None] = mapped_column(ForeignKey("media_assets.id"))
    search_query: Mapped[str] = mapped_column(Text)
    result_rank: Mapped[int] = mapped_column(Integer)
    source_page_url: Mapped[str | None] = mapped_column(Text)
    direct_image_url: Mapped[str | None] = mapped_column(Text, index=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    rights_status: Mapped[str] = mapped_column(String(40), default="unknown")
    original_width: Mapped[int | None] = mapped_column(Integer)
    original_height: Mapped[int | None] = mapped_column(Integer)
    provider_result_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    download_status: Mapped[str] = mapped_column(String(40), default="downloaded")
    download_error: Mapped[str | None] = mapped_column(Text)
    detected_topic_json: Mapped[str] = mapped_column(Text, default="{}")
    quality_score: Mapped[float] = mapped_column(Float)
    style_score: Mapped[float] = mapped_column(Float)
    novelty_score: Mapped[float] = mapped_column(Float)
    caption_potential_score: Mapped[float] = mapped_column(Float)
    source_risk_score: Mapped[float] = mapped_column(Float)
    final_rank_score: Mapped[float] = mapped_column(Float, index=True)
    hard_rejection_reason: Mapped[str | None] = mapped_column(Text)
    soft_warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    score_components_json: Mapped[str] = mapped_column(Text, default="{}")
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    style_profile_id: Mapped[int | None] = mapped_column(ForeignKey("style_profiles.id"))
    start_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class Proposal(Base, TimestampMixin):
    __tablename__ = "proposals"
    __table_args__ = (UniqueConstraint("channel_id", "planned_publish_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    generation_run_id: Mapped[int] = mapped_column(ForeignKey("generation_runs.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    candidate_image_id: Mapped[int] = mapped_column(ForeignKey("candidate_images.id"), index=True)
    caption_slate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_slates.id"), index=True
    )
    backup_candidate_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    planned_publish_at: Mapped[str] = mapped_column(String(40), index=True)
    scheduled_publish_at: Mapped[str | None] = mapped_column(String(40), index=True)
    recommended_caption: Mapped[str] = mapped_column(Text)
    alternative_captions_json: Mapped[str] = mapped_column(Text)
    caption_rationale: Mapped[str] = mapped_column(Text, default="")
    caption_confidence: Mapped[float | None] = mapped_column(Float)
    caption_reference_post_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    factual_uncertainty_warning: Mapped[str | None] = mapped_column(Text)
    final_caption: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    selection_reason: Mapped[str] = mapped_column(Text)
    style_score: Mapped[float] = mapped_column(Float)
    novelty_score: Mapped[float] = mapped_column(Float)
    quality_score: Mapped[float] = mapped_column(Float)
    closest_historical_matches_json: Mapped[str] = mapped_column(Text, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    rights_decision: Mapped[str | None] = mapped_column(String(40), index=True)
    rights_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_post_id: Mapped[str | None] = mapped_column(String(200), index=True)
    external_post_url: Mapped[str | None] = mapped_column(Text)
    scheduled_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProposalEvent(Base):
    __tablename__ = "proposal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    old_value_json: Mapped[str] = mapped_column(Text, default="{}")
    new_value_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaptionFeedback(Base):
    __tablename__ = "caption_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    candidate_image_id: Mapped[int] = mapped_column(ForeignKey("candidate_images.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(40), index=True)
    generated_caption: Mapped[str] = mapped_column(Text)
    preferred_caption: Mapped[str | None] = mapped_column(Text)
    preferred_structure: Mapped[str | None] = mapped_column(String(40), index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    image_verdict: Mapped[str | None] = mapped_column(String(40), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    publisher: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    confirmation_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    planned_publish_at: Mapped[str] = mapped_column(String(40))
    screenshot_paths_json: Mapped[str] = mapped_column(Text, default="[]")
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(80))
    input_record_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    request_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    structured_output_json: Mapped[str] = mapped_column(Text, default="{}")
    token_usage_json: Mapped[str] = mapped_column(Text, default="{}")
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), index=True)
    error_summary: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BlockedSource(Base):
    __tablename__ = "blocked_sources"
    __table_args__ = (UniqueConstraint("source_type", "value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="user blocked")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RepresentationRecord(Base):
    __tablename__ = "representation_records"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "entity_type",
            "entity_id",
            "field",
            "purpose",
            "provider",
            "model",
            "model_version",
            "source_content_hash",
            "configuration_hash",
            name="uq_representation_identity",
        ),
        Index(
            "ix_representation_lookup",
            "channel_id",
            "entity_type",
            "entity_id",
            "purpose",
            "active",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(80))
    modality: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    model_version: Mapped[str] = mapped_column(String(100))
    dimensions: Mapped[int] = mapped_column(Integer)
    vector_count: Mapped[int] = mapped_column(Integer, default=1)
    dtype: Mapped[str] = mapped_column(String(40), default="float32")
    serialized_data: Mapped[bytes] = mapped_column(LargeBinary)
    normalized: Mapped[bool] = mapped_column(Boolean, default=True)
    source_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RepresentationSet(Base):
    __tablename__ = "representation_sets"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "scope",
            "purpose",
            "plan_hash",
            name="uq_representation_set_plan",
        ),
        Index(
            "ix_representation_set_resolution",
            "channel_id",
            "scope",
            "purpose",
            "active",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    purpose: Mapped[str] = mapped_column(String(80), index=True)
    modality: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    model_version: Mapped[str] = mapped_column(String(100))
    configuration_json: Mapped[str] = mapped_column(Text, default="{}")
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    expected_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    stale_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    supersedes_set_id: Mapped[int | None] = mapped_column(ForeignKey("representation_sets.id"))
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepresentationSetItem(Base):
    __tablename__ = "representation_set_items"
    __table_args__ = (
        UniqueConstraint(
            "representation_set_id",
            "entity_type",
            "entity_id",
            "field",
            name="uq_representation_set_item",
        ),
        Index(
            "ix_representation_set_item_checkpoint",
            "representation_set_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    representation_set_id: Mapped[int] = mapped_column(
        ForeignKey("representation_sets.id"), index=True
    )
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(80))
    source_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_locator_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    representation_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("representation_records.id"), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntelligenceActivation(Base):
    __tablename__ = "intelligence_activations"
    __table_args__ = (
        Index(
            "ix_intelligence_activation_lookup",
            "channel_id",
            "resource_type",
            "target",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    target: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    resource_id: Mapped[str] = mapped_column(String(100))
    previous_resource_id: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    gate_results_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelPolicyRule(Base):
    __tablename__ = "channel_policy_rules"
    __table_args__ = (
        Index(
            "ix_channel_policy_active",
            "channel_id",
            "active",
            "priority",
            "rule_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    rule_type: Mapped[str] = mapped_column(String(80), index=True)
    scope: Mapped[str] = mapped_column(String(80), default="channel")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    rule_text: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80), default="creator")
    version_hash: Mapped[str] = mapped_column(String(64), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntelligenceRetrievalRun(Base):
    __tablename__ = "intelligence_retrieval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    query_identity: Mapped[str] = mapped_column(String(128), index=True)
    query_type: Mapped[str] = mapped_column(String(80), index=True)
    candidate_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_images.id"), index=True
    )
    reference_media_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    modification_text: Mapped[str | None] = mapped_column(Text)
    structured_constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    style_profile_id: Mapped[int | None] = mapped_column(ForeignKey("style_profiles.id"))
    profile_version: Mapped[int | None] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(String(64))
    retrieval_configuration_json: Mapped[str] = mapped_column(Text)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    representation_sets_json: Mapped[str] = mapped_column(Text, default="{}")
    cache_diagnostics_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    error_summary: Mapped[str | None] = mapped_column(Text)


class RetrievalEvidenceRecord(Base):
    __tablename__ = "retrieval_evidence_records"
    __table_args__ = (
        Index(
            "ix_retrieval_evidence_selected",
            "retrieval_run_id",
            "selected",
            "selected_rank",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    retrieval_run_id: Mapped[int] = mapped_column(
        ForeignKey("intelligence_retrieval_runs.id"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[int] = mapped_column(Integer)
    retrieval_channel: Mapped[str] = mapped_column(String(80), index=True)
    raw_score: Mapped[float] = mapped_column(Float)
    normalized_score: Mapped[float] = mapped_column(Float)
    fusion_score: Mapped[float] = mapped_column(Float)
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)
    policy_score: Mapped[float] = mapped_column(Float, default=0.0)
    diversity_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_cluster: Mapped[str | None] = mapped_column(String(128), index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    selected_rank: Mapped[int | None] = mapped_column(Integer)
    evidence_role: Mapped[str | None] = mapped_column(String(80), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class CaptionSlate(Base):
    __tablename__ = "caption_slates"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    candidate_image_id: Mapped[int] = mapped_column(ForeignKey("candidate_images.id"), index=True)
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"), index=True)
    retrieval_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("intelligence_retrieval_runs.id"), index=True
    )
    model_run_id: Mapped[int | None] = mapped_column(ForeignKey("model_runs.id"), index=True)
    editorial_brief_json: Mapped[str] = mapped_column(Text)
    raw_output_json: Mapped[str] = mapped_column(Text)
    generation_configuration_json: Mapped[str] = mapped_column(Text)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    token_usage_json: Mapped[str] = mapped_column(Text, default="{}")
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaptionCandidateRecord(Base):
    __tablename__ = "caption_candidate_records"
    __table_args__ = (
        UniqueConstraint("caption_slate_id", "rank", name="uq_caption_slate_rank"),
        UniqueConstraint("derivation_key", name="uq_caption_candidate_derivation"),
        Index("ix_caption_candidate_display", "caption_slate_id", "displayed", "display_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    caption_slate_id: Mapped[int] = mapped_column(ForeignKey("caption_slates.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(40), default="und")
    structure: Mapped[str] = mapped_column(String(80), index=True)
    editorial_angle: Mapped[str] = mapped_column(String(120), index=True)
    visible_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    uncertainty_json: Mapped[str] = mapped_column(Text, default="[]")
    prohibited_claim_checks_json: Mapped[str] = mapped_column(Text, default="{}")
    historical_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    feedback_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    generator_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verifier_result_json: Mapped[str] = mapped_column(Text, default="{}")
    eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    exclusion_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    generation_index: Mapped[int] = mapped_column(Integer, default=1)
    grounding_score: Mapped[float] = mapped_column(Float, default=0.0)
    policy_score: Mapped[float] = mapped_column(Float, default=0.0)
    style_score: Mapped[float] = mapped_column(Float, default=0.0)
    novelty_score: Mapped[float] = mapped_column(Float, default=0.0)
    rotation_score: Mapped[float] = mapped_column(Float, default=0.0)
    positive_feedback_score: Mapped[float] = mapped_column(Float, default=0.0)
    negative_feedback_risk: Mapped[float] = mapped_column(Float, default=0.0)
    pairing_score: Mapped[float] = mapped_column(Float, default=0.0)
    preference_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    displayed: Mapped[bool] = mapped_column(Boolean, default=False)
    display_order: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    replacement_text: Mapped[str | None] = mapped_column(Text)
    decision_latency_ms: Mapped[float | None] = mapped_column(Float)
    origin: Mapped[str] = mapped_column(String(40), default="generated", index=True)
    parent_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(80), default="generator")
    source_proposal_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_events.id"), index=True
    )
    derivation_key: Mapped[str | None] = mapped_column(String(128))
    feature_schema_version: Mapped[str | None] = mapped_column(String(80), index=True)
    feature_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    feature_snapshot_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    representation_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("representation_records.id")
    )
    taxonomy_version: Mapped[str | None] = mapped_column(String(80))
    verifier_version: Mapped[str | None] = mapped_column(String(80))
    ranker_model_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("preference_model_versions.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaptionExposure(Base):
    __tablename__ = "caption_exposures"
    __table_args__ = (
        UniqueConstraint("proposal_id", "caption_slate_id", name="uq_proposal_slate_exposure"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    caption_slate_id: Mapped[int] = mapped_column(ForeignKey("caption_slates.id"), index=True)
    candidate_image_id: Mapped[int] = mapped_column(ForeignKey("candidate_images.id"), index=True)
    ordered_candidate_ids_json: Mapped[str] = mapped_column(Text)
    displayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    selected_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id")
    )
    final_caption: Mapped[str | None] = mapped_column(Text)
    decision_type: Mapped[str | None] = mapped_column(String(80))
    decision_latency_ms: Mapped[float | None] = mapped_column(Float)
    interface_version: Mapped[str] = mapped_column(String(80), default="runway-generator-v1")


class PairwisePreference(Base):
    __tablename__ = "pairwise_preferences"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_pairwise_preference_idempotency"),
        Index(
            "ix_pairwise_preference_training",
            "channel_id",
            "target",
            "learning_split",
            "feature_schema_version",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"), index=True)
    candidate_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_images.id"), index=True
    )
    preferred_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id")
    )
    preferred_text: Mapped[str] = mapped_column(Text)
    dispreferred_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id")
    )
    dispreferred_text: Mapped[str] = mapped_column(Text)
    preference_source: Mapped[str] = mapped_column(String(80), index=True)
    label_source: Mapped[str] = mapped_column(String(40), default="human", index=True)
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    policy_version: Mapped[str | None] = mapped_column(String(64))
    experiment_id: Mapped[str | None] = mapped_column(String(80), index=True)
    target: Mapped[str] = mapped_column(String(40), default="caption", index=True)
    source_proposal_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_events.id"), index=True
    )
    source_exposure_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_exposures.id"), index=True
    )
    source_event_key: Mapped[str | None] = mapped_column(String(128), index=True)
    derivation_version: Mapped[str] = mapped_column(String(80), default="decision-derivation-v1")
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    preferred_features_json: Mapped[str] = mapped_column(Text, default="{}")
    dispreferred_features_json: Mapped[str] = mapped_column(Text, default="{}")
    context_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    feature_schema_version: Mapped[str | None] = mapped_column(String(80), index=True)
    feature_snapshot_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    group_key: Mapped[str | None] = mapped_column(String(128), index=True)
    taxonomy_version: Mapped[str | None] = mapped_column(String(80))
    verifier_version: Mapped[str | None] = mapped_column(String(80))
    style_profile_version: Mapped[int | None] = mapped_column(Integer)
    representation_sets_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieval_configuration_hash: Mapped[str | None] = mapped_column(String(64))
    ranker_configuration_hash: Mapped[str | None] = mapped_column(String(64))
    learning_split: Mapped[str] = mapped_column(String(40), default="development", index=True)
    source_study_response_id: Mapped[int | None] = mapped_column(
        ForeignKey("blind_study_responses.id"), index=True
    )


class FeedbackSignal(Base):
    __tablename__ = "feedback_signals"
    __table_args__ = (
        Index("ix_feedback_signal_context", "channel_id", "target", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_feedback_signal_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"), index=True)
    candidate_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_images.id"), index=True
    )
    target: Mapped[str] = mapped_column(String(40), index=True)
    verdict: Mapped[str] = mapped_column(String(40), index=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), default="creator")
    policy_version: Mapped[str | None] = mapped_column(String(64))
    experiment_id: Mapped[str | None] = mapped_column(String(80), index=True)
    source_proposal_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_events.id"), index=True
    )
    source_caption_feedback_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_feedback.id"), index=True
    )
    source_event_key: Mapped[str | None] = mapped_column(String(128), index=True)
    derivation_version: Mapped[str] = mapped_column(String(80), default="feedback-normalization-v1")
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntelligenceAgentRun(Base):
    __tablename__ = "intelligence_agent_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_intelligence_agent_run_key"),
        Index(
            "ix_intelligence_agent_run_lookup",
            "channel_id",
            "capability",
            "status",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    run_key: Mapped[str] = mapped_column(String(128))
    capability: Mapped[str] = mapped_column(String(100), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    input_json: Mapped[str] = mapped_column(Text)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntelligenceAgentStep(Base):
    __tablename__ = "intelligence_agent_steps"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "sequence",
            "attempt",
            name="uq_intelligence_agent_step_attempt",
        ),
        Index(
            "ix_intelligence_agent_step_lookup",
            "agent_run_id",
            "sequence",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("intelligence_agent_runs.id"), index=True)
    parent_step_id: Mapped[int | None] = mapped_column(ForeignKey("intelligence_agent_steps.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    capability: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    input_json: Mapped[str] = mapped_column(Text)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    artifact_type: Mapped[str | None] = mapped_column(String(80))
    artifact_id: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreferenceDataset(Base):
    __tablename__ = "preference_datasets"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_preference_dataset_content"),
        Index(
            "ix_preference_dataset_lookup",
            "channel_id",
            "target",
            "status",
            "created_at",
        ),
    )

    dataset_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    target: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(40), default="frozen", index=True)
    feature_schema_version: Mapped[str] = mapped_column(String(80), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(80))
    split_seed: Mapped[int] = mapped_column(Integer)
    configuration_json: Mapped[str] = mapped_column(Text)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    split_counts_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreferenceDatasetItem(Base):
    __tablename__ = "preference_dataset_items"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "pairwise_preference_id",
            name="uq_preference_dataset_pair",
        ),
        Index("ix_preference_dataset_split", "dataset_id", "split", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("preference_datasets.dataset_id"), index=True
    )
    pairwise_preference_id: Mapped[int] = mapped_column(
        ForeignKey("pairwise_preferences.id"), index=True
    )
    split: Mapped[str] = mapped_column(String(40), index=True)
    group_key: Mapped[str] = mapped_column(String(128), index=True)
    position: Mapped[int] = mapped_column(Integer)
    preferred_features_json: Mapped[str] = mapped_column(Text)
    dispreferred_features_json: Mapped[str] = mapped_column(Text)
    strength: Mapped[float] = mapped_column(Float)
    snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)


class PreferenceModelVersion(Base):
    __tablename__ = "preference_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "target",
            "artifact_hash",
            name="uq_preference_model_artifact",
        ),
        Index(
            "ix_preference_model_resolution",
            "channel_id",
            "target",
            "active",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    target: Mapped[str] = mapped_column(String(40), index=True)
    algorithm: Mapped[str] = mapped_column(String(80), default="pairwise_logistic")
    status: Mapped[str] = mapped_column(String(40), default="trained", index=True)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("preference_datasets.dataset_id"), index=True
    )
    parent_model_id: Mapped[int | None] = mapped_column(ForeignKey("preference_model_versions.id"))
    feature_schema_version: Mapped[str] = mapped_column(String(80), index=True)
    feature_names_json: Mapped[str] = mapped_column(Text)
    parameters_json: Mapped[str] = mapped_column(Text)
    training_configuration_json: Mapped[str] = mapped_column(Text)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    calibration_json: Mapped[str] = mapped_column(Text, default="{}")
    label_count: Mapped[int] = mapped_column(Integer)
    minimum_label_count: Mapped[int] = mapped_column(Integer)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnotationRefreshRun(Base):
    __tablename__ = "annotation_refresh_runs"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "annotation_version",
            "plan_hash",
            name="uq_annotation_refresh_plan",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    annotation_version: Mapped[str] = mapped_column(String(80), index=True)
    prompt_version: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    configuration_json: Mapped[str] = mapped_column(Text, default="{}")
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    expected_count: Mapped[int] = mapped_column(Integer)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnotationRefreshItem(Base):
    __tablename__ = "annotation_refresh_items"
    __table_args__ = (
        UniqueConstraint(
            "annotation_refresh_run_id",
            "post_id",
            name="uq_annotation_refresh_item",
        ),
        Index(
            "ix_annotation_refresh_checkpoint",
            "annotation_refresh_run_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    annotation_refresh_run_id: Mapped[int] = mapped_column(
        ForeignKey("annotation_refresh_runs.id"), index=True
    )
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    source_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    post_annotation_id: Mapped[int | None] = mapped_column(ForeignKey("post_annotations.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BlindStudy(Base):
    __tablename__ = "blind_studies"
    __table_args__ = (UniqueConstraint("study_key", name="uq_blind_study_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    study_key: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(40), default="caption", index=True)
    baseline_identity: Mapped[str] = mapped_column(String(200))
    challenger_identity: Mapped[str] = mapped_column(String(200))
    seed: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    split_policy_json: Mapped[str] = mapped_column(Text, default="{}")
    preregistration_json: Mapped[str] = mapped_column(Text, default="{}")
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    response_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BlindStudyCase(Base):
    __tablename__ = "blind_study_cases"
    __table_args__ = (
        UniqueConstraint("blind_study_id", "case_key", name="uq_blind_study_case"),
        Index("ix_blind_study_case_order", "blind_study_id", "display_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blind_study_id: Mapped[int] = mapped_column(ForeignKey("blind_studies.id"), index=True)
    case_key: Mapped[str] = mapped_column(String(128))
    candidate_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_images.id"), index=True
    )
    media_asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), index=True)
    split: Mapped[str] = mapped_column(String(40), index=True)
    group_key: Mapped[str] = mapped_column(String(128), index=True)
    first_caption: Mapped[str] = mapped_column(Text)
    second_caption: Mapped[str] = mapped_column(Text)
    first_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id"), index=True
    )
    second_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("caption_candidate_records.id"), index=True
    )
    order_token: Mapped[str] = mapped_column(String(40))
    hidden_label_json: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    selection_rationale_json: Mapped[str] = mapped_column(Text, default="{}")
    display_order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BlindStudyResponse(Base):
    __tablename__ = "blind_study_responses"
    __table_args__ = (UniqueConstraint("blind_study_case_id", name="uq_blind_study_response"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    blind_study_case_id: Mapped[int] = mapped_column(ForeignKey("blind_study_cases.id"), index=True)
    choice: Mapped[str] = mapped_column(String(40))
    reviewer_kind: Mapped[str] = mapped_column(String(40), default="creator")
    reviewer_label: Mapped[str] = mapped_column(String(80), default="local-creator")
    review_session: Mapped[str] = mapped_column(String(128), index=True)
    acceptable_choices_json: Mapped[str] = mapped_column(Text, default="[]")
    edited_final_caption: Mapped[str | None] = mapped_column(Text)
    image_verdict: Mapped[str | None] = mapped_column(String(40))
    caption_verdict: Mapped[str | None] = mapped_column(String(40))
    pairing_verdict: Mapped[str | None] = mapped_column(String(40))
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str | None] = mapped_column(Text)
    decision_time_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActiveLearningBatch(Base):
    __tablename__ = "active_learning_batches"
    __table_args__ = (UniqueConstraint("batch_key", name="uq_active_learning_batch_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    batch_key: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(40), index=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    seed: Mapped[int] = mapped_column(Integer)
    configuration_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="selected", index=True)
    selection_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActiveLearningSelection(Base):
    __tablename__ = "active_learning_selections"
    __table_args__ = (
        UniqueConstraint(
            "active_learning_batch_id",
            "entity_type",
            "entity_id",
            name="uq_active_learning_selection",
        ),
        Index(
            "ix_active_learning_priority",
            "active_learning_batch_id",
            "priority_rank",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    active_learning_batch_id: Mapped[int] = mapped_column(
        ForeignKey("active_learning_batches.id"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[int] = mapped_column(Integer)
    group_key: Mapped[str] = mapped_column(String(128), index=True)
    split: Mapped[str] = mapped_column(String(40), index=True)
    priority_rank: Mapped[int] = mapped_column(Integer)
    uncertainty_score: Mapped[float] = mapped_column(Float)
    diversity_score: Mapped[float] = mapped_column(Float)
    rationale_json: Mapped[str] = mapped_column(Text)
    label_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)


class ImageGenerationRun(Base):
    __tablename__ = "image_generation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("intelligence_agent_runs.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    model_version: Mapped[str] = mapped_column(String(100))
    capability: Mapped[str] = mapped_column(String(80))
    creative_brief_json: Mapped[str] = mapped_column(Text)
    reference_media_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    reference_rights_json: Mapped[str] = mapped_column(Text, default="[]")
    consent_state: Mapped[str] = mapped_column(String(40), default="not_required")
    instructions: Mapped[str] = mapped_column(Text, default="")
    negative_instructions: Mapped[str] = mapped_column(Text, default="")
    seed: Mapped[int | None] = mapped_column(Integer)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    provider_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), index=True)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GeneratedAssetLineage(Base):
    __tablename__ = "generated_asset_lineage"
    __table_args__ = (
        UniqueConstraint("generation_run_id", "media_asset_id", name="uq_generated_asset_lineage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    generation_run_id: Mapped[int] = mapped_column(
        ForeignKey("image_generation_runs.id"), index=True
    )
    media_asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), index=True)
    candidate_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_images.id"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    safety_result_json: Mapped[str] = mapped_column(Text, default="{}")
    rights_result_json: Mapped[str] = mapped_column(Text, default="{}")
    reference_similarity: Mapped[float | None] = mapped_column(Float)
    historical_similarity: Mapped[float | None] = mapped_column(Float)
    duplicate_result_json: Mapped[str] = mapped_column(Text, default="{}")
    annotation_json: Mapped[str] = mapped_column(Text, default="{}")
    ranking_json: Mapped[str] = mapped_column(Text, default="{}")
    review_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    creator_decision: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntelligenceExperiment(Base):
    __tablename__ = "intelligence_experiments"

    experiment_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True)
    parent_experiment_id: Mapped[str | None] = mapped_column(String(80), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    baseline_commit: Mapped[str] = mapped_column(String(64))
    candidate_commit: Mapped[str | None] = mapped_column(String(64))
    datasets_json: Mapped[str] = mapped_column(Text)
    configuration_json: Mapped[str] = mapped_column(Text)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    components_json: Mapped[str] = mapped_column(Text, default="[]")
    model_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    prompt_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    hard_gates_json: Mapped[str] = mapped_column(Text, default="{}")
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    selection_decision: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
