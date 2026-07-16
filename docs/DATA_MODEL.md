# Data model

Core tables are `channels`, `capture_runs`, `raw_post_records`, `posts`, `media_assets`,
`post_media`, `post_annotations`, `annotation_corrections`, `style_profiles`, `similarity_edges`,
`search_runs`, `candidate_images`, `generation_runs`, `proposals`, `proposal_events`, `model_runs`,
and `audit_events`.

External IDs are unique per channel when present; permalinks provide a secondary idempotency key.
Media is content-addressed by SHA-256. Generated/model values are append-only or evented; reviewed
corrections overlay original annotations rather than replacing them.
