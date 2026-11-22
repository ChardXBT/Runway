# Spec 00 — Principles and invariants

These hold across every other spec. If a later spec seems to conflict with one of these, this
file wins and the conflict should be reported, not resolved silently.

## Terms

- **Profile** — how an installation runs: `local` or `hosted`. Set by `RUNWAY_PROFILE`.
- **Workspace** — the unit of isolation. Owns accounts, content, drafts, posts, tokens, media.
- **Principal** — who is acting: a human user, an agent key, or the system worker.
- **Post** — one piece of content intended for one or more platform accounts.
- **Target** — one post going to one account. Each target publishes independently.
- **Attempt** — one try at publishing one target. Append-only.
- **Release** — the human (or policy) decision that a target may be sent to the platform.

## Invariants carried over from today's Runway

1. **Nothing reaches a platform without an explicit release.** Accepting a proposal, saving a
   draft or scheduling a time are local actions. Release is separate and recorded.
2. **Model runtimes cannot publish.** The intelligence harness registry stays free of any
   publishing or platform-mutation capability.
3. **Never report success that was not re-read from the platform.** `verified` is a distinct
   state reached only by reading the result back.
4. **An uncertain outcome is never retried automatically.** If a request may have been
   delivered, the attempt becomes `uncertain` and waits for reconciliation.
5. **Tokens never enter logs, API responses or the public repository.**
6. **No blended cross-platform metrics.** Each number keeps its platform, definition, source
   and collection time.
7. **Mock providers are a mode, never a fallback.**
8. **Existing routes keep working.** `/review`, `/lineup`, `/queue`, `/connector`, `/archive`,
   `/catalogue`, `/profile`, `/activity`, `/settings`, both `/health` endpoints, the
   `AgentRuntime` protocol and `run-runway.ps1` are compatibility surfaces for the Supervisor
   and Muse.
9. **Production data is never touched by tests.**
10. **No stealth automation, CAPTCHA solving or challenge bypass.**

## New invariants for the expansion

11. **Every row belongs to a workspace**, in both profiles.
12. **Every route receives a `Principal`** and authorizes against it, in both profiles.
13. **Privacy is always sent explicitly** to every platform. A missing value is a validation
    error, never a platform default. (YouTube's default became `public` on 2026-11-02.)
14. **Capability is per account, not per platform.** Whether publishing is ready depends on the
    account's scopes, the app's approval status and whose account it is.
15. **Platform facts are data with a date.** Limits such as "100 uploads per day" live in one
    module with the date they were read, and the UI shows live values from the platform where an
    endpoint exists (`content_publishing_limit`, `creator_info`).
16. **Agents propose; humans release**, unless a workspace policy explicitly grants an agent
    key the release scope. On TikTok the platform's rules make human completion mandatory.
17. **The `local` profile must never become harder to run.** No login, no cloud account, no
    paid service is required to use Runway on one's own machine.

## Non-goals

- Changing the intelligence engine, retrieval, or training pipeline.
- Replacing the Community-post browser publisher.
- Threads, X, LinkedIn, Facebook Pages, or any fourth platform.
- Direct messages.
- Billing. It is named in spec 05 as a later step only.
- Video editing or transcoding beyond validation. Runway rejects files a platform will not
  accept and says why; it does not fix them in phase 1.
