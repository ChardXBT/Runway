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

`proposals.scheduled_publish_at` is the reserved external slot. It is separate from the generation
placeholder so unreviewed options do not occupy the calendar. The allocator enforces at most one
RunWay reservation per Toronto local date and has no fixed future-horizon limit.

`publish_attempts` is the persisted FIFO outbox and publisher audit trail. It stores the
publisher/state, a hash of the exact image/caption/time/channel payload, expiry/submission/completion
times, verification evidence, screenshot paths, and external ID/URL when known. The legacy
diagnostic CLI also uses a hashed one-time confirmation token. Plain tokens and Google credentials
are never persisted.

The canonical intelligence flywheel adds explicit representation-set,
agent-run, preference-dataset/model, annotation-refresh, blind-study,
active-learning, activation, and decision-provenance lifecycles. Normalized
feedback is now the canonical learning read path; legacy `caption_feedback`
remains preserved and is reconciled idempotently.

See [`INTELLIGENCE_DATA_MODEL.md`](INTELLIGENCE_DATA_MODEL.md) for table
relationships, active-set rules, immutable feature snapshots, label provenance,
training rules, schema fingerprinting, and the current production lifecycle
state.
