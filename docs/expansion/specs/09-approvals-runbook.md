# Spec 09 — Platform approvals runbook

What the **owner** must do outside the code, in what order, and what each step unlocks. An
implementing agent must not create accounts, accept terms, submit review forms or sign up for
services on its own; each of these is the owner's action or needs the owner's explicit go-ahead.

Facts are in the research files. Anything marked "test" is in the acceptance list of
[`../IMPLEMENTATION-PLAN.md`](../IMPLEMENTATION-PLAN.md).

## Stage 1 — Owner's own accounts (no review, $0)

### YouTube

1. Google Cloud project (exactly one for Runway). Enable YouTube Data API v3 and YouTube
   Analytics API.
2. OAuth consent screen: external. Add the owner's Google account.
3. Set publishing status to **In production** (do not submit for verification yet). Purpose:
   avoid refresh tokens that expire after 7 days in Testing. Expect an "unverified app" warning
   once at consent. Test Y-1.
4. OAuth client, type Web application, redirect
   `http://127.0.0.1:8000/api/platforms/youtube/oauth/callback`.
5. Client id and secret go into the private instance (spec 07), never the public repository.
6. Connect in Runway; then enable publishing, which requests `youtube.upload`.

Unlocks: read, analytics, comments, replies (if enabled), video and Shorts upload, native
scheduling. Ceiling: 100 uploads per day, 10,000 other units per day.

### Instagram

1. The Instagram account must be Professional (Creator or Business).
2. Meta developer app with the Instagram product, "API setup with Instagram login".
3. Add the owner's Instagram account as a tester (or generate a token in the dashboard).
4. Redirect URI as above with `instagram`.
5. For publishing: Cloudflare R2 bucket and an API token limited to that bucket (free tier),
   credentials into the private instance. Without it, Instagram stays on the manual workflow.

Unlocks: read, insights, comments, replies, publishing to the owner's account. Not unlocked:
comment webhooks (need Advanced Access).

### TikTok

1. TikTok for Developers app. Add Login Kit, Display API, and Content Posting API with the
   upload (inbox) configuration.
2. Create a **sandbox**; add the owner's TikTok account as a target user.
3. HTTPS redirect: the public edge URL plus `/api/platforms/tiktok/oauth/callback`.
4. Run test T-2: does an inbox upload from the sandbox reach the owner's TikTok inbox?

Outcomes:

- T-2 passes: TikTok works as "send draft to my inbox". Good enough for one creator.
- T-2 fails: choose between the manual workflow (free, already built) and a vendor adapter
  (decision for the owner; the cheapest option found is a vendor whose first two connected
  accounts are free, which needs an account sign-up).

Public direct posting is **not** obtainable at this stage, by TikTok's own rules.

### Optional: public edge

Cloudflare Tunnel (or equivalent) exposing only `/hooks/*` and OAuth callback paths. Needed for
TikTok connect and for any webhook. Confirm it is free before enabling.

## Stage 2 — Preparing for outside users

Everything here is groundwork the three reviews share. Do it once.

1. A domain for the product.
2. A real public website on that domain: what Runway does, screenshots, pricing or "free beta",
   contact. TikTok rejects "a landing or login page".
3. Privacy Policy and Terms of Service on the same domain, linked visibly from the home page
   without opening a menu. Content requirements in spec 05 §5.
4. The hosted app running on a subdomain of the same domain (the reviews check that the demo
   video's domain matches).
5. Working disconnect, delete-account and delete-workspace flows.
6. An app name and icon that do not reference any platform's brand.
7. A registered business, if Meta's Business Verification turns out to require one (open
   question Q5). Find this out before starting the Meta review.

## Stage 3 — Reviews

Order chosen by how much each unlocks versus how likely it is to pass early:

| # | Review | Unlocks | Needs | Stated duration | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | Google OAuth verification (sensitive scopes) | No unverified warning; more than 100 users | Verified domain, home page, privacy policy, demo video showing consent with the client id visible and each scope in use, written justification per scope | "up to 10 days" | No security assessment for sensitive scopes |
| 2 | Meta App Review + Business Verification | Other people's Instagram accounts; comment webhooks | Screencasts per permission, test account, Business Verification of the operating business | Not found | Set the app Live after approval |
| 3 | TikTok app review (production) | Login and read for any user | Website, policies, demo video recorded in sandbox on the product's domain | Not stated | "Apps must not be built for private or personal use" |
| 4 | TikTok Content Posting audit | Public direct posting | The mandatory posting screen implemented exactly; usage estimates | Not found | Rejected for utilities that post to the developer's own accounts |
| 5 | YouTube API compliance audit and quota extension | More than 100 uploads per day | Audit form; compliance with Developer Policies (30-day refresh, deletion, single project, required links) | Not stated | Only when the ceiling is actually near |

Before each submission, re-read that platform's current requirements. They change, and this
runbook is dated 2026-11-14.

## What each review will look for in the product

- **Google:** the app requests only the scopes it uses; the privacy policy describes Google
  data handling; the demo shows every scope doing something visible.
- **Meta:** each permission is demonstrated end to end by a reviewer using supplied test
  credentials; data deletion works.
- **TikTok:** the posting screen follows the guidelines point by point (research/tiktok §7);
  the product is for a wide audience; no branding is added to content; nothing is sent before
  the user consents.

## Things only the owner can decide or do

- Create the developer apps and accept each platform's developer terms.
- Choose the product domain and name.
- Decide whether to register a business.
- Approve any service sign-up, including free ones (identity provider, storage, tunnel, vendor).
- Approve any spend.
- Submit each review.
