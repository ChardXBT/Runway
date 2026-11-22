# Runway Expansion — research and specifications

Branch `research/expansion-20261113`. Written 2026-11-13/14. Documentation only: no application
code changed on this branch.

**Goal being specified** (owner's words): keep Runway working with Muse, make it dual-use, and
reach the end state "Runway [Expansion]":

- connect real YouTube, Instagram and TikTok accounts;
- add video publishing, real-time sync and hosted multi-workspace support.

This folder is written so that an implementing model (Opus or similar) can build from it without
re-doing the research. Every decision states why it was chosen and what was rejected.

## How to read this

| Order | File | What it is |
| --- | --- | --- |
| 1 | this file | Summary, decisions at a glance, what is not known |
| 2 | [`01-AUDIT.md`](01-AUDIT.md) | What the earlier research got right, got wrong, and missed; code problems found |
| 3 | [`research/`](research/) | Source-cited fact files (one per platform, plus build-vs-buy, hosting, private data, agent interface) |
| 4 | [`specs/`](specs/) | The specifications, numbered in build order |
| 5 | [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md) | Phases, work packages, acceptance tests |
| 6 | [`OPEN-QUESTIONS.md`](OPEN-QUESTIONS.md) | What could not be determined, and decisions only the owner can make |

Evidence labels used in the research files:

- **[Official]** — read on the platform's or vendor's own page on the date given.
- **[Community]** — reported by developers or vendors; plausible, not authoritative.
- **[Unverified]** — could not be confirmed; treat as a hypothesis to test.
- **[Inference]** — my reasoning from the facts, not a quoted fact.

Nothing here has been tested against a live account. No developer app was created, no form was
submitted, and no money was spent. The first live connection of each platform is an acceptance
test (see the plan).

## The short version

1. **YouTube publishing is unblocked and should be built first, directly on the official API.**
   Google's revision history says that since 2 November 2026 uploads from unaudited projects are
   no longer locked to private, and uploads have their own bucket of 100 per day per project.
   This is 12 days old and Google's own issue tracker recorded conflicting documentation in
   October, so it must be proven with one live upload before anything depends on it.
2. **Instagram publishing to the owner's own account needs no review** (Standard Access covers
   accounts you own or manage). The real constraint is that Instagram fetches media from a public
   URL; a short-lived signed URL on Cloudflare R2 is the specified answer. Serving *other
   people's* accounts needs Meta App Review and Business Verification.
3. **TikTok cannot be solved by engineering alone.** Public posting needs an audit, and TikTok
   lists "utility tools that upload content to accounts you or your team manage" and apps for
   "internal groups or private use" as not acceptable. Until Runway is a real public product,
   the options are: send drafts to the TikTok inbox, use a vendor that already holds the audit,
   or keep the manual workflow. The spec supports all three behind one interface.
4. **Dual use is one codebase with two deployment profiles**, not a fork: `local` (today's
   loopback, single implicit workspace, SQLite, Muse) and `hosted` (login, workspaces,
   Postgres). Every table gets a workspace id in both profiles.
5. **Agents (Muse) get a real interface**: versioned REST with scoped keys, plus an MCP server.
   Agents may draft and propose; releasing a post to a platform stays a human action unless a
   workspace explicitly allows otherwise.
6. **Private data and a public repo**: use a private companion repository, not git-crypt in the
   public repo and not a fork (GitHub does not allow private forks of public repos).
7. **"Real-time" is honest per platform**: push where the platform offers it (YouTube upload
   notifications, TikTok publish status, Instagram comments after review), fast polling
   elsewhere, and server-sent events from Runway to the browser.

## Decisions at a glance

| # | Decision | Chosen | Main rejected alternative | Spec |
| --- | --- | --- | --- | --- |
| D1 | Dual-use shape | One codebase, `local` and `hosted` profiles, workspace id everywhere | Separate hosted fork | [specs/01](specs/01-dual-use-and-tenancy.md) |
| D2 | Publishing integration | Direct platform adapters behind a `Publisher` port, with an optional vendor adapter | Vendor-only, or direct-only | [specs/03](specs/03-publishing-engine.md) |
| D3 | Media handling | Local disk in `local`; Cloudflare R2 with direct browser upload in `hosted`; signed URL only for Instagram | Public media server on the app host | [specs/02](specs/02-media-pipeline.md) |
| D4 | Scheduling and jobs | Runway's own durable job table and worker, on SQLite and Postgres | Temporal, Celery, Procrastinate | [specs/03](specs/03-publishing-engine.md) |
| D5 | Real-time | Webhook inbox + adaptive polling + server-sent events | WebSockets, third-party realtime service | [specs/04](specs/04-realtime-sync.md) |
| D6 | Hosted data store | Postgres, shared schema, row-level security | Database per workspace | [specs/01](specs/01-dual-use-and-tenancy.md) |
| D7 | Login and workspaces | Managed identity provider behind a thin seam (Clerk first choice) | Hand-rolled auth | [specs/05](specs/05-hosted-platform.md) |
| D8 | Agent interface | REST v1 + scoped keys + MCP server; propose-not-release | Unauthenticated loopback API (today) | [specs/06](specs/06-agent-interface.md) |
| D9 | Private instance data | Private companion repo + release assets for the database | git-crypt in public repo; private fork | [specs/07](specs/07-private-instance-data.md) |
| D10 | Instagram login flavour | Stay on Instagram Login now; add Unified Login as a second adapter before hosted launch | Migrate immediately | [specs/08](specs/08-platform-adapters.md) |

## What I could not figure out

Full list in [`OPEN-QUESTIONS.md`](OPEN-QUESTIONS.md). The ones that could change the plan:

- Whether the 2 November 2026 YouTube change really applies to a brand-new unaudited project.
- Whether TikTok's sandbox allows inbox (draft) uploads, and whether a draft-only app can pass
  TikTok app review.
- Whether a solo developer without a registered company can pass Meta Business Verification.
- Whether Instagram accepts Cloudflare R2 signed URLs for media fetch.
- Whether TikTok's US joint venture changes anything for developers. I found no documented
  API split, which is not proof there is none.
- Muse's side of the integration. Its code is not in this repository, so the agent interface is
  specified from Runway's side only.

## Scope note

`WHAT_IS_NEXT.md` (written 2026-10-29) argued that Runway as a general creator tool is a weak
startup and recommended freezing publisher work. This expansion goes the other way at the
owner's explicit request. That business question is not re-argued here; the specs are written so
the work is useful for the owner's own channels even if the hosted product never launches.
