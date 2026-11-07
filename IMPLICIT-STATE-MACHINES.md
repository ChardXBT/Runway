# Implicit State Machine Audit — runway

Scope: tracked source files under `src/runway/` (Python backend) and `apps/web/`
(Next.js frontend). Lock files, tests, fixtures, and generated assets skipped.

The repo has **one** declared state model (`runway/domain/enums.py` +
`runway/domain/state_machine.py`, covering `ProposalStatus` and `RunStatus`).
Every other lifecycle in the system is encoded implicitly, and even the two
declared machines are partially bypassed. Findings are ordered by risk.

---

## Publish-attempt lifecycle (YouTube scheduling)

**States** (≈10, raw strings): `prepared` → `queued` → `submitting` →
`verified` | `submitted_unverified`; side-exits `blocked_session`,
`failed_before_submission`, `expired`, `invalidated`, `superseded`.

**Encoded as**: `PublishAttempt.status` (`src/runway/db/models.py:367`) with no
enum and no transition table, plus four nullable timestamps that re-encode the
same phase: `prepared_at`/`expires_at`/`submitted_at`/`completed_at`
(`models.py:375-378`). "Terminal" is equivalently `completed_at IS NOT NULL`.

**Invalid combos**: `status="submitting"` with `submitted_at IS NULL`;
`status="verified"` with `completed_at IS NULL`; `status="expired"` while a
queue read still counts the attempt as active; `status="prepared"` with
`expires_at` already past. `submitting` is inside the "active" set read by the
queue (`src/runway/publishing/queue.py:135`), so a crash mid-submit leaves a
row that the coordinator counts as in-flight forever but never re-executes
(`next_queued_attempt_id` only picks `queued`, `youtube.py:1522-1527`).

**Transitions**: `prepared` written at `src/runway/publishing/youtube.py:1363`,
`:1495`; `superseded` at `:1361`, `:1493`, `:2033`; `queued` at `:1562`;
`blocked_session` at `:2186`; `failed_before_submission` at `:2159`; `expired`
at `:2218`; `invalidated` at `:2232`, `:2287`; `submitting` at `:2239`, `:2294`;
`verified`/`submitted_unverified` at `:2348`. Read/decision sites:
`queue.py:135` (active set), `:157` (blocked detection joined to
`Proposal.status`), `youtube.py:1358`, `:1479`, `:1490`, `:1525`, `:1541`,
`:1585`, `:2029`, `:2184`, `:2215`, `:2277`; also queried as a set in
`src/runway/services/settings.py:90-94`. Frontend mirrors the vocabulary in
`apps/web/components/lineup-calendar.tsx:823`, `:1683`
(`latest_publish_attempt.status === "failed_before_submission"`).

**Risk**: HIGH. This is the money path for external scheduling. The queue
coordinator reconstructs in-flight state from raw strings after process
restart; a missed or invented status string silently stalls the lineup
(stuck `submitting` rows) or lets a superseded attempt run twice
(double-schedule to YouTube). UI retry affordances key off the same literals.

---

## Versioned-resource activation (RepresentationSet / PreferenceModelVersion / AnnotationRefreshRun)

**States**: planned → backfilling → ready → active → superseded | rolled_back;
plus `stale`, `backfilled`, `failed`, `partial` (set-level), and per-item
`pending`/`running`/`complete`/`failed`/`stale`.

**Encoded as**: each table carries **four parallel encodings** of the same
state: `status` string, `active` boolean, `activated_at`/`superseded_at`/
`validated_at` timestamps, and denormalized `completed_count`/`failed_count`/
`stale_count` counters — see `models.py:506-517` (RepresentationSet),
`:1140-1155` (PreferenceModelVersion), `:1181-1189` (AnnotationRefreshRun).
Activation is real only when `active IS TRUE **and** status == "active"`;
both are written by hand at every transition site.

**Invalid combos**: `active=TRUE, status="superseded"` (or `"stale"`,
`"rolled_back"`); `status="active", active=FALSE`; `active=TRUE` with
`activated_at IS NULL`; `status="ready"` with `failed_count > 0`. The code
itself knows these can occur — `src/runway/intelligence/doctor.py:475` flags
`representation_set.active and representation_set.status != "active"` as a
doctor-checkable corruption, and scoring/rollback both defensively re-check the
pair (`preference_models.py:614`, `representation_sets.py:427`).

**Transitions**: RepresentationSet — `ready`/`backfilling`/`failed` derived
from item counts at `src/runway/intelligence/representation_sets.py:310-315`,
`backfilled` at `:591-595`; activate quad-write at `:372-379`; rollback at
`:452-458`; `stale` + `active=False` at `src/runway/intelligence/embeddings.py:1212-1229`.
PreferenceModelVersion — activate at `src/runway/captions/preference_models.py:578-585`,
rollback at `:629-635`. AnnotationRefreshRun — `running`/`ready`/`failed` at
`src/runway/intelligence/annotation_refresh.py:165,220,225,371`, activate at
`:267-269`, `backfilled` at `:369`. Reads are split-brain: some sites filter
`active.is_(True)` (`embeddings.py:1251`, activation queries), some
`status == "active"` (`embeddings.py:994`, `:1479`, `profile.py:878`,
`preference_models.py:678`), some both (`preference_models.py:677-678`,
`doctor.py:1163,1175,1195`).

**Risk**: HIGH. `status` and `active` diverging by one forgotten write makes
the wrong embedding set / preference model serve production traffic, or makes
`activate()` believe no set is active while one still resolves — the RuntimeError
guards at `representation_sets.py:369` and `preference_models.py:574-575` then
block activation entirely (stuck system). Rollback targets are found via
`supersedes_set_id`/`parent_model_id` chains that assume the flags agree.

---

## Proposal status: declared machine bypassed, timestamps as parallel signals

**States**: the declared `ProposalStatus` enum (11 values,
`src/runway/domain/enums.py:51-61`) with a transition table
(`src/runway/domain/state_machine.py:7-43`).

**Encoded as**: `Proposal.status` string column (`models.py:304`) **plus**
nullable timestamps that re-encode the same phases: `approved_at`,
`rejected_at`, `scheduled_verified_at` (`models.py:313-317`).

**Invalid combos**: `status="approved", approved_at IS NULL`;
`status="rejected", rejected_at IS NULL`;
`status="externally_scheduled", scheduled_verified_at IS NULL`. The code
exploits the ambiguity — `src/runway/intelligence/candidate_diversity.py:137-138`
filters `status == "needs_review" AND approved_at.is_(None)` because the status
string alone is not trusted to mean "not yet approved".

**Transitions**: sanctioned path `proposals/service.py:_transition`
(`:1617-1633`) calls `require_transition`. But raw-string writes bypass the
guard: `candidate_diversity.py:142` and `:186` assign `status = "cancelled"`
directly (also emitting hand-built events). Read sites re-encode the enum as
raw strings across services: `editorial/service.py:220`,
`intelligence/shadow_editorial.py:57`, `intelligence/profile.py:224`,
`intelligence/retrieval.py:920`, `platforms/overview.py:211`,
`candidate_diversity.py:137,174`, and `publishing/queue.py:158`. The frontend
re-implements the machine with literals throughout
`apps/web/components/lineup-calendar.tsx` (`:107-110`, `:178`, `:211`,
`:362-363`, `:419`, `:432-444`, `:820-823`, `:1117-1130`, `:1354`, `:1681-1683`)
— including compound decisions like
`status === "publish_failed" && latest_publish_attempt.status ===
"failed_before_submission"` (`:822-823`).

**Risk**: MEDIUM-HIGH. The enum gives a false sense of safety: any new raw
write path skips `require_transition`, and a status renamed in the enum must be
edited in ~15 scattered literal sites (backend + frontend) that the type
checker cannot find. Cross-table combos (proposal status AND attempt status)
decide whether the UI offers retry — a missed literal means a stuck or
un-actionable scheduled post.

---

## Intelligence agent run/step lifecycle

**States**: `running` → `completed` | `failed` | `abstained` (run and step).

**Encoded as**: `IntelligenceAgentRun.status`/`IntelligenceAgentStep.status`
raw strings (`models.py:1015`, `:1050`), with terminal-ness dual-encoded as
`completed_at IS NOT NULL` (`models.py:1024`, `:1060`).

**Invalid combos**: `status="running"` with `completed_at` set;
`status="completed"` with `completed_at IS NULL`. The system's own doctor
treats this divergence as a known corruption class:
`src/runway/intelligence/doctor.py:1453-1454` computes
`terminal = run.status in {"completed","failed","abstained"}` and reports when
`terminal != (run.completed_at is not None)`; same for steps at `:1489`.

**Transitions**: `running` at `agent_harness.py:289,320,467,518`;
`completed` at `:386,396,578,612`; `abstained` at `:588-592`;
`failed` at `:862,877`. Reads: existence guards `not in {"completed","abstained"}`
at `:275`, `:449`, `:698`; step filtering at `:827`; captions service reads the
terminal literal at `captions/service.py:652`.

**Risk**: MEDIUM. An exception between the status write and the
`completed_at` write produces rows that are simultaneously "terminal" and
"in-flight" depending on which signal a reader trusts — e.g. the idempotency
guard at `:275` blocks re-runs of a "completed" run that has no `completed_at`,
while doctor flags it critical.

---

## Content draft / draft-target lifecycle

**States**: `draft` → `scheduled` → `published` | `partially_published` |
`archived` (draft); `pending` → `published` (target).

**Encoded as**: `ContentDraft.status` (`models.py:1683`) plus
`scheduled_for IS NULL` as a second "is scheduled" signal; `DraftTarget.status`
(`models.py:1699`) plus `published_at`/`published_url`/`published_content_id`
presence (`drafts.py:250-253`).

**Invalid combos**: `status="scheduled"` with `scheduled_for IS NULL` (the API
accepts explicit `status` at `platforms/drafts.py:200-201` while
`clear_schedule` nulls the timestamp at `:195`); `status="draft"` with
`scheduled_for` set; `status="archived"` then re-derived to `"published"` by
`_recompute_status` (`:216-224`), which only ever upgrades and cannot un-publish.

**Transitions**: `scheduled` inferred from timestamp at `drafts.py:202-203`;
`archived` at `:212`; `published`/`partially_published` derived by counting
child targets at `:220-224`; target `published` at `:250`. Reads: list filter
`!= "archived"` at `:116` and `overview.py:192`; account-connect gate
`ConnectedAccount.status == "connected"` at `:155`; sync counts
`DraftTarget.status == "published"` at `sync.py:534`. Frontend re-encodes all
five draft states in `apps/web/components/drafts-workspace.tsx:35-46` and
`target.status === "published"` at `:606`.

**Risk**: MEDIUM. Aggregate status is re-derived only on `mark_published`;
any other write path (archive, target failure, manual status via the API's
free-form `status` param, `platform_routes.py:130` constrains it to
`draft|scheduled` but the service takes any string) leaves parent and children
in contradictory states — an archived draft that still reports
"partially_published", or a "published" draft whose targets were never sent.

---

## Account connectivity & sync-run lifecycle

**States**: account `connected` | `disconnected` | `needs_reauth`; sync run
`running` → `completed` | `failed` | `partial`; per-step statuses inside
`steps_json`; `last_sync_status` mirrors the last run outcome onto the account.

**Encoded as**: `ConnectedAccount.status` + `last_sync_status` raw strings
(`models.py:1479`, `:1485`); `SyncRun.status` (`models.py:1652`). Token
presence/expiry (`expires_at`, `sync.py:311-316`) is yet another connectivity
signal that can disagree with `status`.

**Invalid combos**: `status="connected"` with `last_sync_status="failed"`;
`status="connected"` with an expired token; `needs_reauth` cleared by a raw
`status="connected"` write (`platform_routes.py:320`) that bypasses any token
check. Orphaned `running` rows are recovered only by a startup sweep
(`sync.py:86-92`), so a crashed process leaves runs that look live until restart.

**Transitions**: `connected` at `platform_routes.py:320`; `disconnected` at
`:503`; `needs_reauth` at `sync.py:341`; run `running`/`failed`/`completed`/
`partial` at `sync.py:128,92,198,260-271`; `last_sync_status` at `:367`.
Reads: `sync.py:104,864`, `drafts.py:155`, `cli/main.py:2285`,
`overview.py:58,107`, `platforms/inbox.py` capability gating. Frontend
mirrors: `accounts-manager.tsx:195-199,291-301,395-396,471,708`,
`dashboard-overview.tsx:113,367-369`, `analytics/page.tsx:27`.

**Risk**: MEDIUM. `needs_reauth` vs `connected` decides whether the app
attempts API calls; a stale `connected` after token expiry produces repeated
failing syncs, while `running` rows surviving a crash double-report in-flight
work. The frontend polls raw literals (`"running"`, `"queued"` — which the
backend never writes for SyncRun), so vocabulary drift degrades UI silently.

---

## Inbox comment moderation

**States**: `new` → `read` → `answered` | `escalated` | `ignored`
(declared as `COMMENT_STATUSES`/`Literal` at `platforms/inbox.py:39-41` and
duplicated at `api/platform_routes.py:98` and `inbox-workspace.tsx:26` —
three literal lists, no shared enum).

**Encoded as**: `CommentRecord.status` string (`models.py:1608`) **plus**
`has_owner_reply` boolean (`models.py:1609`) that re-encodes "answered".
The canonical unanswered predicate requires BOTH signals to agree:
`has_owner_reply IS FALSE AND status IN (new, read, escalated)`
(`inbox.py:44-49`).

**Invalid combos**: `status="answered", has_owner_reply=FALSE` — hides from
the unanswered queue a comment that looks answered in the UI;
`status="new", has_owner_reply=TRUE` — a comment the owner did reply to is
filtered out of "needs reply" while still showing badge "new". Nothing keeps
the pair in sync; `set_status` (`inbox.py:244-253`) writes the string without
touching the flag, and the flag is set on sync when an owner reply is
detected without touching the string.

**Transitions**: `answered` at `inbox.py:375`; arbitrary status via
`set_status` (validated against the tuple only); frontend PATCH via
`platform_routes.py:823` and locally mutates `{status: "answered",
has_owner_reply: true}` at `inbox-workspace.tsx:512`.

**Risk**: MEDIUM-LOW. Divergence mis-sorts the moderation queue (missed or
phantom unanswered comments); triplicated status lists drift independently.

---

## Publisher connection state (event-log derived)

**States**: `disabled` | `unchecked` | `connected` | `needs_attention` | `stale`.

**Encoded as**: no column at all — derived per request from the newest
`AuditEvent` rows of type `youtube_session_validated` /
`youtube_session_validation_failed`, a `valid` flag in JSON details, and a
24-hour clock check (`youtube.py:1213-1304`; `state` computed at `:1296` as
`"stale" if stale else ("connected" if valid else "needs_attention")`).

**Invalid combos**: any past event sequence produces a `state`; `stale` only
applies when `valid` (a stale *failure* still reports `needs_attention`, not
`stale`) — subtle branches of the same implicit function. The resume gate
reads it as `state != "connected" or valid is not True`
(`queue.py:60`), double-checking a pair the producer builds inconsistently.

**Transitions**: implicit — new audit events at `youtube.py` session checks;
staleness arrives with the clock. Read: `queue.py:59-63` gates queue resume.

**Risk**: MEDIUM-LOW. State exists only as a computed projection; two callers
computing it differently (or at different clock instants) disagree, and the
`stale` state can never be persisted/observed historically.

---

## Caption-candidate funnel flags & exposure decision

**States**: funnel `eligible → displayed → selected`, with `edited` as a
lateral marker; exposure `decision_type` ∈ `selected | accepted | edited |
rejected` (raw strings passed through the API).

**Encoded as**: four independent booleans on `CaptionCandidateRecord`
(`models.py:720,735,737,738`) instead of a phase enum; `CaptionExposure.
decision_type` free-form string.

**Invalid combos**: `edited=TRUE, selected=FALSE` — used intentionally
(original replaced by an edit, `exposures.py:214`) **and** `selected=TRUE,
edited=TRUE` — used for the edit-created record (`exposures.py:378-379`), so
`edited` means two different things; `selected=TRUE, displayed=FALSE` is
representable but nonsensical; `eligible=FALSE` candidates still selectable.
`decision_type` accepts any string at `exposures.py:223` though only four
values are ever produced by callers (`proposals/service.py:703,767,847,996,
1021,1110`) and comparisons branch on subsets (`exposures.py:199,229,233`).

**Transitions**: `displayed` at `exposures.py:79`; `edited` at `:214`, `:379`;
`selected` at `:226`, `intelligence/fusion.py:126`. Reads: preference-pair
construction `exposures.py:229-250`; doctor SQL `doctor.py:892`.

**Risk**: MEDIUM-LOW. Bad flag combos corrupt the pairwise-preference training
data (e.g. an "edited-away" original counted as a selected positive), which is
hard to detect downstream.

---

## Run-status vocabulary divergence

**States**: every job/run table repeats the same lifecycle shape with
different vocabularies and enforcement.

**Encoded as**: `RunStatus` enum (`enums.py:10-15`) covers CaptureRun,
SearchRun, GenerationRun — yet `capture/service.py:81` writes
`status="dry_run"`, a value outside the enum. Other run tables use raw
strings: `IntelligenceRetrievalRun` `completed`/`failed`
(`retrieval.py:1146,1159`), `MultimodalRerankRun` `shadow_completed`
(`reranking.py:199`), `ModelRun` `completed`/`abstained`
(`captions/service.py:837,1555`), `ImageGenerationRun`, `CaptionSlate`
`completed`/`ready` (`captions/service.py:1397`; `studies.py:936`), and the
item tables use `complete` (not `completed`) —
`representation_sets.py:340,444,525,565`, `annotation_refresh.py:359`.
Failure is also encoded without status: `catalog/service.py:242` treats
`CaptureRun.status == "failed" OR error_summary IS NOT NULL` as failed —
i.e. `error_summary` presence is a parallel failure flag, so a run can be
"completed and failed" at once.

**Invalid combos**: `status="completed", error_summary NOT NULL`;
`status="dry_run"` passes no enum validation and is invisible to
`RunStatus`-based filters (`capture/service.py:283-295`, `:513-528` exclude it
or not depending on hand-built lists); `"complete"` vs `"completed"` string
typos silently match nothing.

**Transitions**: scattered — `capture/service.py:117,168,538,546`;
`discovery/service.py:164,181,246,262`; `generation/service.py:247,262`;
`retrieval.py:1146,1159`.

**Risk**: MEDIUM-LOW. Same conceptual machine, ≥4 vocabularies; filters
hand-list literals so a missed variant silently drops rows (e.g. `dry_run`
runs excluded from "latest run" queries, or `partial`-style states invisible
to `completed`-only aggregations like `studies.py:936`).

---

## Study/batch lifecycle derived from counters

**States**: BlindStudy/FrontierArenaStudy `planned → in_progress / in_review →
completed`; `exported` lateral; ActiveLearningBatch `selected`-init;
ActiveLearningSelection `label_status` `pending`-init;
ComposedRetrievalExample `candidate | reviewed`.

**Encoded as**: status strings (`models.py:1233`, `:1318`, `:1351`, `:1427`)
recomputed from denormalized counters — `study.status = "completed" if
study.response_count == study.case_count else "in_review"`
(`frontier_arena.py:433`) — or derived from a sibling boolean —
`status = "reviewed" if reviewed else "candidate"` plus a separate `reviewed`
column written together (`composed_retrieval.py:113-115`, `models.py:1427-1429`).

**Invalid combos**: `status="completed"` with `response_count < case_count`
(counters drift from the derived status they produced); `status="reviewed",
reviewed=FALSE` on ComposedRetrievalExample once either column is updated
alone; `status="exported"` studies later mutated back (`studies.py:436-437`
only upgrades `planned`).

**Transitions**: `studies.py:285,436-437,532,535,888`;
`frontier_arena.py:228,347,433-434`. Reads: `studies.py:936`,
`doctor.py:923` (`slate.status in {"completed","ready"}`).

**Risk**: LOW-MEDIUM. Derived statuses diverge from counters silently; human
labeling state (`label_status`, `review_status` on GeneratedAssetLineage —
`pending/accepted/rejected` enumerated only inside the doctor check at
`doctor.py:1548-1552`) has no single definition site, so new statuses can be
introduced by any writer and go uncounted.

---

## Summary

| # | Machine | Encoding | Severity |
|---|---------|----------|----------|
| 1 | PublishAttempt lifecycle | ~10 raw strings + 4 timestamps | HIGH |
| 2 | Versioned activation (3 tables) | status + active bool + timestamps + counters | HIGH |
| 3 | Proposal status | enum declared, but raw-string writes bypass it + timestamp signals | MED-HIGH |
| 4 | Agent run/step | status string XOR completed_at | MEDIUM |
| 5 | ContentDraft/DraftTarget | aggregate + scheduled_for null-signal | MEDIUM |
| 6 | Account/SyncRun | status + last_sync_status + token expiry | MEDIUM |
| 7 | CommentRecord | status + has_owner_reply | MED-LOW |
| 8 | Publisher connection | audit-event log + 24h clock | MED-LOW |
| 9 | Caption funnel | 4 booleans + decision_type string | MED-LOW |
| 10 | Run-status vocabulary | ≥4 divergent vocabularies, `error_summary` as flag | MED-LOW |
| 11 | Study/batch statuses | counter/boolean-derived | LOW |

The dominant pattern: **the codebase's own `doctor` module is a catalog of
the invalid combos these implicit machines allow** (status↔timestamp
divergence, `active`↔`status` divergence, statuses outside the only
enumerated set). Declaring per-table enums plus a tiny transition guard —
like the one `ProposalStatus` already has but only partially uses — would
turn today's runtime corruptions into import-time errors.
