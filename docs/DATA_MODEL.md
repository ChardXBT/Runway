# Data model

Core tables are `channels`, `capture_runs`, `raw_post_records`, `posts`, `media_assets`,
`post_media`, `post_annotations`, `annotation_corrections`, `style_profiles`, `similarity_edges`,
`search_runs`, `candidate_images`, `generation_runs`, `proposals`, `proposal_events`,
`caption_feedback`, `publish_attempts`, `model_runs`, and `audit_events`.

External IDs are unique per channel when present; permalinks provide a secondary idempotency key.
Media is content-addressed by SHA-256. Generated/model values are append-only or evented; reviewed
corrections overlay original annotations rather than replacing them.

`caption_feedback` is append-only learning evidence. It stores the generated and preferred text,
caption structure, verdict, reason codes, image verdict, note, candidate, proposal, and timestamp.

`publish_attempts` stores the publisher/state, a hash of the one-time confirmation token, a hash of
the exact image/caption/time/channel payload, expiry/submission/completion times, verification
evidence, screenshot paths, and external ID/URL when known. Plain confirmation tokens and Google
credentials are never persisted.
