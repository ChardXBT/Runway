Job name: **rwy-connect** (connect-flow hardening, after wave 4)

Make the owner's connect step boring and safe. Specs: `specs/00-principles.md`,
`specs/08-platform-adapters.md` §1-§5, `specs/04-realtime-sync.md` §6 (public edge),
`specs/07-private-instance-data.md` (where secrets go), `specs/09-approvals-runbook.md` stage 1,
`IMPLEMENTATION-PLAN.md` "Live acceptance tests", `research/recheck-20261122.md`.

## Work packages

**`runway doctor platforms`** (CLI; also `GET /api/v1/doctor/platforms` for the web settings page,
read-only, owner only). For each of YouTube, Instagram, TikTok print, in plain words:
- client id present / client secret present (never print values; show only "set" and where it
  came from: environment, instance secrets, settings file);
- the exact redirect URI Runway will send (`{oauth_redirect_base_url}/api/platforms/{platform}/oauth/callback`,
  computed by the same function the OAuth code uses, not re-implemented) and whether it is HTTPS
  where the platform requires it (TikTok and Meta require HTTPS; Google allows `http://localhost`
  and `http://127.0.0.1` for desktop/web testing - check what the code uses);
- for TikTok and webhooks: whether `RUNWAY_PUBLIC_EDGE_URL` is set, and an optional
  `--check-edge` that requests `{edge}/hooks/health` (a new tiny signature-free route that returns
  `{ok: true}` and is on the edge allow-list) - this is the ONLY network call and it goes to the
  owner's own edge, never to a platform;
- requested scopes per adapter vs scopes granted on each connected account; token state (expires,
  refresh expires, needs_reauth), app status settings (`youtube_app_status`,
  `instagram_app_status`, `tiktok_app_status`, `youtube_posts_api_access`), publishing mode flags;
- the private instance state (active/standby) and whether secrets come from it;
- a final "What is missing" list with the next action in one line each. Exit code 0 when ready,
  1 when something required is missing. `--json` for machines. Tests with fake settings for every
  branch; no network in tests (the edge check uses an injected HTTP client).

**`docs/CONNECT_ACCOUNTS.md`** - the owner's runbook, plain language, numbered steps:
1. Prerequisites (private instance with `runway instance init`, sops/age; Tailscale Funnel for
   TikTok and webhooks exposing only `/hooks/*` and the OAuth callback paths; optional R2 bucket
   for Instagram).
2. Google Cloud: project, enable YouTube Data API v3 and YouTube Analytics API, OAuth consent
   screen set to "In production" (why: 7-day refresh tokens in Testing), web client, the exact
   redirect URI from `runway doctor platforms`, scopes Runway requests (read them from the code),
   where to put client id/secret (instance secrets, exact variable names from settings).
3. Meta: developer app, "Instagram API with Instagram Login" product, the owner's account as an
   Instagram tester, redirect URI, scopes, variable names; note that the Facebook Login for
   Business path exists for resumable upload and how to switch to it.
4. TikTok: developer app, Login Kit + Content Posting API (upload/inbox), sandbox with the owner's
   account as target user, redirect URI on the Funnel HTTPS host, webhook URL, variable names.
5. Connect each account in the web app; run `runway doctor platforms` until it says ready.
6. Turning on publishing (the exact settings) and what each app-status value unlocks.
7. Live acceptance tests Y-1..Y-5, I-1..I-4, T-1, T-2 written as owner steps: what to do, what to
   look at, what result to record in `docs/VERIFICATION_REPORT.md`, and what to do if it fails
   (the fallbacks in IMPLEMENTATION-PLAN).
8. Troubleshooting (redirect URI mismatch, unverified app screen, 7-day tokens, 9007 errors,
   TikTok `spam_risk_too_many_pending_share`, WebSub lease, standby machine).
Every console label you name must match the platform's current wording as recorded in
`docs/expansion/research/`; where the research does not record it, say "the console's
credentials page" rather than inventing a label.

**Capability document.** Regenerate `docs/PLATFORM_CAPABILITIES.md` (find the generator in the
codebase) so it reflects the new publishers and gates.

## Acceptance

ruff, format, mypy clean; tests for every doctor branch fail before the change; `tests/unit` and
affected tests pass; CI green on the pushed branch.
