# Audit of the earlier research and the current code

Audited: `docs/PLATFORM_RESEARCH.md`, `docs/PLATFORM_CAPABILITIES.md`,
`docs/VERIFICATION_REPORT.md`, `docs/CURRENT_LIMITATIONS.md`, `docs/FUTURE_PUBLISHER.md`,
`WHAT_IS_NEXT.md` (all dated 2026-10-17 to 2026-10-29), and the code under
`src/runway/platforms/`, `src/runway/api/`, `src/runway/db/models.py`.

There was no separate research branch. The earlier research lives on `main`.

## 1. What the earlier research got right

| Claim | Status on 2026-11-14 |
| --- | --- |
| YouTube: 10,000 units/day plus separate buckets of 100 calls/day for `search.list` and `videos.insert` | Confirmed word for word on Google's quota page |
| YouTube: no API for Community posts | Still true; nothing in the 2025–2026 revision history adds one |
| YouTube policy III.E.2: do not combine data across content owners | Confirmed |
| Instagram: media must be on a public URL; JPEG only; two login flavours | Confirmed |
| Instagram: Standard Access is enough for an account you own | Confirmed |
| TikTok: unaudited clients post private-only; comments only in the Business API | Confirmed |
| TikTok: redirect URI must be HTTPS | Not re-read (TikTok pages block one of my tools); still consistent with app review rules |
| The honest "nothing is live-verified" framing | Correct, and still true |

## 2. What it got wrong or is now out of date

| Earlier statement | Correction | Source |
| --- | --- | --- |
| YouTube publishing is simply "supported, not implemented" | It omitted the rule that mattered most: until 2 Nov 2026, uploads from unaudited projects were locked private. That rule is now lifted. | [research/youtube.md](research/youtube.md) §2 |
| Instagram publishing is `approval_required` | For the owner's own account it is not. No review is needed; only a public media URL is. The matrix state should distinguish "own account" from "other people's accounts". | [research/instagram.md](research/instagram.md) §1 |
| Instagram limit "100 posts per 24 h (50 for carousels)" | The page now says 100 per 24 h and that a carousel counts as one post. A second line on the same page still says 50. Treat as 50 until `content_publishing_limit` says otherwise. | [research/instagram.md](research/instagram.md) §3 |
| "Runway implements Instagram Login only" (as the modern choice) | Meta now has "Instagram API with Unified Login" and says new features ship only there. Instagram Login still works; no end date. | [research/instagram.md](research/instagram.md) §6 |
| Instagram "Scheduling: unsupported … Not built" | Correct that the API has no scheduling, but this is true for every competitor. They all hold the post and publish at the time. It is a job-queue feature, not a blocker. | [specs/03](specs/03-publishing-engine.md) |
| TikTok "FILE_UPLOAD … PULL_FROM_URL requires a verified domain" (complete) | Missing: unaudited clients also require the *account itself* to be private and allow at most 5 posting users per 24 h. | [research/tiktok.md](research/tiktok.md) §2 |

## 3. What it missed entirely

1. **TikTok's audit excludes personal and internal tools.** This decides the whole TikTok
   strategy and was not recorded anywhere.
2. **TikTok upload-to-inbox (drafts)**, scope `video.upload`: the creator finishes the post in
   the TikTok app. Not mentioned.
3. **TikTok mandatory posting screen**: nickname display, privacy dropdown with no default,
   interaction toggles off by default, commercial-content disclosure, exact consent strings.
   These are audit requirements and shape the UI.
4. **TikTok webhooks, sandbox (10 accounts, 5 sandboxes), photo posts, token lifetimes** (page
   read on 2026-11-14 says access 2 hours, refresh 180 days, refresh token rotates).
5. **Google OAuth verification**: "Testing" status issues refresh tokens that die after 7 days;
   100 refresh tokens per account per client; sensitive-scope verification needs a public
   homepage, privacy policy on the same domain, verified domain and a demo video.
6. **YouTube data rules for a hosted product**: refresh or delete stored data every 30 days,
   delete within 7 days of revocation, exactly one API project per client.
7. **YouTube push notifications** (WebSub) for uploads and title/description changes.
8. **Instagram webhooks**: app must be Live; `comments` needs Advanced Access; account must be
   public; Meta retries for 36 hours.
9. **Instagram details needed to publish**: container polling (once a minute, at most 5
   minutes), containers expire in 24 hours, 400 containers per 24 h, Reel and Story file specs,
   `is_ai_generated`, trial reels, `alt_text`.
10. **Everything about hosting**: login, workspaces, tenancy, token custody, media storage,
    job queue, real-time delivery, costs.
11. **Buy instead of build**: only Ayrshare was named. Cheaper vendors with free tiers exist.
12. **Compliance duties** of a hosted app: privacy policy, deletion flows, revocation handling.
13. **AI disclosure flags** on all three platforms (`containsSyntheticMedia`, `is_ai_generated`,
    `is_aigc`). Relevant because Muse generates content.
14. **How to keep a personal instance's data when the code repository is public.**

## 4. Problems found in the code

These are not bugs introduced by this branch; they are things the specs must fix or work around.
File references are to `main` at `5b3bad8`.

| # | Finding | Where | Why it matters for the expansion |
| --- | --- | --- | --- |
| C1 | Drafts cannot carry media. `content_drafts.media_path` exists but no request model or route sets it, and there is no upload endpoint. | `src/runway/api/platform_routes.py:114-129`, `src/runway/db/models.py:1680` | Video publishing needs a media pipeline from scratch. |
| C2 | A transient failure while refreshing a token is converted to "authorization expired", which marks the account `needs_reauth`. | `src/runway/platforms/sync.py:319-326` | A network blip disconnects an account. Fatal for scheduled publishing. |
| C3 | Token refresh is not serialized per account across processes. Two workers can refresh the same token. | `src/runway/platforms/sync.py:299-328` | TikTok rotates refresh tokens; the loser's token is dead and the user must reconnect. |
| C4 | No tenancy: 65 tables, none has a workspace column; `connected_accounts` is unique on `(platform, external_account_id)` globally. | `src/runway/db/models.py:1465-1470` | Two workspaces could never connect the same channel; no isolation. |
| C5 | Token store is one Fernet-encrypted JSON file guarded by a file lock. | `src/runway/platforms/tokens.py` | Does not work with multiple hosts or containers; no key rotation. |
| C6 | The only access control is the loopback guard; settings refuse any non-loopback host. | `src/runway/api/security.py`, `src/runway/config/settings.py:100-105` | Hosted mode needs real authentication and must not weaken local mode. |
| C7 | The app is created at import time and runs migrations against whatever `.env` points at. This already migrated the production database by accident once. | `src/runway/api/app.py:149`, `docs/VERIFICATION_REPORT.md` "Disclosure" | New migrations for tenancy are large. Import-time migration must be removed first. |
| C8 | Sync scheduler is a daemon thread inside the API process. | `src/runway/platforms/sync.py:829-872` | Jobs die with the web process; cannot scale; no exactly-once. |
| C9 | Draft and target status are free strings with contradictory combinations possible. | `IMPLICIT-STATE-MACHINES.md` "Content draft / draft-target lifecycle" | Publishing adds many more states; needs a declared state machine. |
| C10 | Two unrelated publishing models: `proposals` + `publish_attempts` (Community posts by browser) and `content_drafts` + `draft_targets` (everything else). | `src/runway/publishing/`, `src/runway/platforms/drafts.py` | The Muse flow ends in the first; video publishing belongs to the second. They need one bridge. |
| C11 | TikTok token lifetime assumptions are untested; provider code was written from documentation only. | `src/runway/platforms/tiktok.py` | Expect first-run surprises. |
| C12 | Capability matrix is global per platform, not per account or per approval state. | `src/runway/platforms/capabilities.py` | "Publishing ready" depends on audit status and on whose account it is. |

## 5. What the earlier research could not have known

Three facts changed after 2026-10-17:

- 2026-11-02: YouTube lifted the private lock for unaudited projects.
- 2026-11-03: `thumbnails.set` accepts custom thumbnails for Shorts.
- Meta's Unified Login migration guide (undated on the page; found on 2026-11-14).

Platform rules move monthly. Every fact file carries the date it was read; re-check anything
older than about 60 days before building on it.
