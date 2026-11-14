# Spec 03 — Publishing engine

Decisions D2 and D4. Research: the three platform files,
[`../research/build-vs-buy.md`](../research/build-vs-buy.md),
[`../research/hosting-and-sync.md`](../research/hosting-and-sync.md) §3.

This is the core of the expansion: a post with media goes to one or more accounts at a planned
time, exactly once, and Runway always knows and shows the truth about what happened.

## 1. Model

Extend the existing `content_drafts` and `draft_targets` rather than adding parallel tables.
They already mean "one piece of content, several platform targets", the UI exists, and sync
already links targets to imported content (`sync.py:_link_published_targets`). In the API and
UI they are called **posts** and **targets** from now on; table names stay to avoid a rename
migration on production data.

```
content_drafts  (+ workspace_id, + created_by, + source ['human'|'agent'|'proposal'],
                 + source_ref, + ai_generated bool, + version int)
draft_media(draft_id, asset_id, position)                       -- new
draft_targets   (+ workspace_id, + post_type, + planned_at, + release_state,
                 + released_by, + released_at, + payload_hash, + state, + state_reason,
                 + external_id, + version int)
publish_jobs(id, workspace_id, target_id, kind, run_at, state, attempts, max_attempts,
             locked_by, locked_until, last_error, idempotency_key unique, created_at)
target_attempts(id, workspace_id, target_id, job_id, adapter, started_at, finished_at,
                outcome, request_json, response_json, external_id, error_code,
                error_detail, resumable_json)                     -- append-only
```

- `planned_at` moves from the draft to each target, so one post can go to YouTube now and
  TikTok tomorrow. `content_drafts.scheduled_for` stays as the default applied to new targets.
- `post_type`: `youtube_video`, `youtube_short`, `instagram_reel`, `instagram_image`,
  `instagram_carousel`, `instagram_story`, `tiktok_video`, `tiktok_draft`.
- `settings_json` (exists) holds the per-platform options, validated by a typed schema per
  `post_type` (spec 08).
- `version` enables optimistic concurrency: every edit sends the version it read and fails with
  409 if it changed. Humans and agents edit the same posts, so last-write-wins is not acceptable.
- `target_attempts` replaces nothing; the existing `platform_actions` ledger keeps serving
  comment replies. The two share the same state vocabulary.

## 2. Target state machine

One declared machine, in `runway.domain.state_machine`, in the same style as the existing
proposal and attempt machines. No free-string status writes (fixes audit C9).

```
draft ──► ready ──► released ──► queued ──► sending ──► processing ──► published ──► verified
            ▲          │            │          │             │
            │          ▼            ▼          ├──► failed_retryable ──► queued (auto, bounded)
            └──── (edit) ◄── cancelled         ├──► failed_final
                                               ├──► uncertain ──► (reconcile) ──► published | failed_final
                                               └──► needs_user  (TikTok inbox draft: creator must finish in app)
```

| State | Meaning | Who moves it on |
| --- | --- | --- |
| `draft` | Being edited; validation may have errors | editor |
| `ready` | Validation clean; time may be set | system on validate |
| `released` | A principal with `posts:release` approved this exact payload | human or allowed key |
| `queued` | A job exists for `planned_at` | system |
| `sending` | Worker is talking to the platform | worker |
| `processing` | Platform accepted the bytes; it is transcoding or moderating | worker |
| `published` | Platform returned an id | worker |
| `verified` | Runway re-read the item from the platform and it matches | worker or sync |
| `needs_user` | Delivered to the creator's TikTok inbox; waiting for them | worker |
| `failed_retryable` | Provably not delivered, or platform said retry | worker |
| `failed_final` | Platform refused; retrying the same payload cannot work | worker |
| `uncertain` | The request may have been delivered; outcome unknown | worker |
| `cancelled` | Withdrawn before sending | human |

Rules:

- Editing content, media, time or settings after `released` returns the target to `ready` and
  clears the release. The release is bound to `payload_hash` (SHA-256 over canonical JSON of
  caption, settings, media hashes, account external id and `planned_at`). The worker recomputes
  the hash before sending and refuses on mismatch. Reason: an approval must mean "this exact
  post", especially when an agent can edit.
- `published` is not the end. `verified` requires a read-back: YouTube `videos.list`, Instagram
  `GET /{media_id}`, TikTok status fetch with `publicaly_available_post_id`.
- The post's own status is derived from its targets by one function and never written directly.

## 3. Jobs and the worker

`publish_jobs` is a small durable queue. One row per thing to do at a time: `send`,
`poll_processing`, `verify`, `reconcile`, plus non-publishing kinds used by other specs
(`probe_media`, `sync_account`, `refresh_token`, `renew_subscription`, `purge`).

Claiming:

- Postgres: `SELECT … WHERE state='pending' AND run_at <= now() ORDER BY run_at
  FOR UPDATE SKIP LOCKED LIMIT 1`, then set `locked_by`, `locked_until = now() + lease`.
- SQLite: the same query without `SKIP LOCKED` inside `BEGIN IMMEDIATE`, with exactly one worker
  process. Local mode has one worker by design.
- A job whose lease expired is **not** blindly re-run. For `send` jobs the worker first looks at
  the latest attempt: if it never reached the "request sent" marker, re-run; otherwise create a
  `reconcile` job. Reason: a crash mid-send is exactly the uncertain case.

The worker is a separate process (`runway worker`), started by the launcher in `local` and as
its own service in `hosted`. The API never publishes inline. This replaces the daemon thread in
the API (audit C8); `PlatformSyncScheduler` becomes a producer of `sync_account` jobs.

Idle behaviour: the worker sleeps until the earliest `run_at` (capped at 30 s) rather than
polling every second, so a scale-to-zero database is not kept awake needlessly.

Why not a queue library: see research/hosting-and-sync §3. In short, Runway must run on SQLite
too, and the hard part (uncertain outcomes) is domain logic no library provides.

## 4. Exactly-once

True exactly-once delivery to a third-party API is impossible; the design aims for
**at-most-once sending with verified outcomes**.

1. **One job per target per release.** `idempotency_key = target_id:payload_hash`, unique.
2. **Write the intent before the call.** Insert the attempt row and commit, then call the
   platform. A crash leaves evidence.
3. **Mark the point of no return.** Each adapter records `request_sent_at` immediately before
   the call that can create the post (YouTube final byte range; Instagram `media_publish`;
   TikTok `init` for direct post).
4. **Classify failures** with the existing `request_may_have_been_delivered` helper
   (`platforms/base.py`): connect errors are safe to retry; read timeouts and dropped
   connections after sending are `uncertain`.
5. **Reconcile before any retry.** The reconcile job asks the platform whether the post exists:
   - YouTube: an interrupted resumable session can be queried (`Content-Range: bytes */N`); a
     completed one is found by listing recent uploads and matching title plus a duration and
     size check.
   - Instagram: read the container's `status_code`; `PUBLISHED` means it went out.
   - TikTok: status fetch by `publish_id`.
   Found: move to `published`. Provably absent: `failed_retryable`. Cannot tell: stay
   `uncertain` and ask the human, showing what was checked.
6. **Bounded automatic retries** only from `failed_retryable`: 3 tries, backoff 1, 5, 20
   minutes, never past `planned_at + 6 h`. After that the human decides.

## 5. Publisher port

```python
class Publisher(Protocol):
    name: str                       # 'youtube_direct', 'instagram_direct', 'tiktok_direct',
                                    # 'tiktok_inbox', 'vendor:<name>', 'assisted'
    def supports(self, post_type: str) -> bool: ...
    def preflight(self, ctx: PublishContext) -> list[Issue]: ...
    def send(self, ctx: PublishContext, progress: Progress) -> SendResult: ...
    def poll(self, ctx: PublishContext) -> PollResult: ...
    def verify(self, ctx: PublishContext) -> VerifyResult: ...
    def reconcile(self, ctx: PublishContext) -> ReconcileResult: ...
```

- `preflight` runs at release time and again just before sending: token valid, required scope
  granted, platform quota available (`content_publishing_limit`, YouTube upload counter, TikTok
  `creator_info`), media valid for the target, privacy set explicitly.
- Each connected account has a `publisher` setting per post type, chosen from the adapters that
  support it. Default is the direct adapter when its capability is ready, otherwise `assisted`.
- `assisted` is today's manual workflow as an adapter: it produces the checklist and waits for
  "mark as published". It is always available and never fails.
- A vendor adapter implements the same port. Nothing else in the system knows whether a post
  went out directly or through a vendor.

Why a port: it makes the TikTok question (own app, inbox draft, vendor, or manual) a setting per
account instead of a fork in the code, and it lets the owner's install and the hosted product
differ only in configuration.

## 6. Scheduling

- A planned time is stored in UTC with the workspace's time zone for display.
- YouTube: two modes. `native` (default): upload now as private with `status.publishAt`, so
  YouTube itself publishes on time even if Runway is down. `held`: upload at the planned time.
  Native needs the upload to finish before the planned time, so the editor requires at least 15
  minutes of lead and warns below an hour for large files.
- Instagram and TikTok have no native scheduling; the job runs at `planned_at`. The container
  for Instagram is created up to 30 minutes early so processing is done by the planned time,
  then `media_publish` is called on time. Containers expire in 24 hours, so never earlier.
- Rate guards before queuing: Instagram posts in the last 24 h against the live limit; YouTube
  uploads against the project's daily 100 (a counter per project per Pacific day, since quota
  resets at midnight Pacific); TikTok roughly 15 per creator per day.
- Missed window: if the worker was down past `planned_at`, a target less than 60 minutes late is
  sent; later than that it moves to `ready` with reason "missed while offline" and the human
  decides. Reason: a post that goes out six hours late without anyone choosing that is worse
  than one that asks.

## 7. Bridge from the Muse and Lineup flow

Today Muse's work ends as a `proposal` accepted into the Lineup for a YouTube Community post
(audit C10). Add one explicit action, "Also post to…", on an accepted proposal: it creates a
post with `source='proposal'`, `source_ref=<proposal id>`, the proposal's image and caption, and
targets the user picks. The proposal's own Community-post path is untouched. Nothing is created
automatically. Reason: keeps the existing contract intact and makes cross-posting a deliberate
choice.

## 8. API (all under `/api/v1`)

| Method and path | Scope | Notes |
| --- | --- | --- |
| `POST /posts` | `posts:write` | Body: title, body, media ids, targets |
| `GET /posts`, `GET /posts/{id}` | `posts:read` | Includes targets, validation issues, capability per target |
| `PATCH /posts/{id}` | `posts:write` | Requires `If-Match: <version>` |
| `PUT /posts/{id}/targets/{tid}` | `posts:write` | Settings, caption override, post type |
| `POST /posts/{id}/targets/{tid}/schedule` | `posts:schedule` | `planned_at` or `now` |
| `POST /posts/{id}/targets/{tid}/release` | `posts:release` | Body must echo `payload_hash` and `confirmed: true` |
| `POST /posts/{id}/targets/{tid}/cancel` | `posts:schedule` | Only before `sending` |
| `POST /posts/{id}/targets/{tid}/reconcile` | `posts:release` | Human-triggered check for `uncertain` |
| `POST /posts/{id}/targets/{tid}/mark-published` | `posts:release` | Assisted adapter and manual resolution |
| `GET /posts/{id}/targets/{tid}/attempts` | `posts:read` | The ledger |

Existing `/api/drafts…` routes stay as thin aliases until the web app has moved.

## 9. User interface

- Post editor: media with per-target validation; caption with per-target override and live
  character and hashtag counts; a settings panel per target (spec 08); time picker per target.
- Release dialog: shows the exact payload per target, the account it goes to, the privacy
  setting in words, and the AI-disclosure flag. One confirmation per target.
- TikTok targets render the mandatory screen from research/tiktok §7 and cannot be released from
  a batch action.
- Every target shows its state in plain language, the last attempt's result, and for
  `uncertain` a "what we checked" list with a link to the platform.
- Calendar: targets appear by `planned_at`; the existing Lineup entries appear alongside,
  read-only, so the creator sees one schedule.

## 10. Acceptance criteria

- Killing the worker at each step of a send (before intent, after intent, mid-upload, after the
  point of no return, after response) never produces a duplicate post against a fake adapter
  that records calls, and always ends in a correct state after restart.
- A read timeout on the final call yields `uncertain`, not `failed`, and no automatic retry.
- Editing a released target invalidates the release.
- Two workers against Postgres never claim the same job (concurrency test).
- A target missing an explicit privacy value cannot be released.
- With no platform credentials at all, the assisted adapter still completes a post end to end.
