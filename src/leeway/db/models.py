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

from leeway.db.base import Base


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
    backup_candidate_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    planned_publish_at: Mapped[str] = mapped_column(String(40), index=True)
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
