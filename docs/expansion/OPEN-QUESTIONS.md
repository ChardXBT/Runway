# Open questions and honest limits

## How this research was done

- One pass on 2026-11-13/14, reading official pages through two fetch tools that return
  model-written extracts of each page. Quotes are as those tools returned them. Before quoting a
  platform's wording back to that platform, re-read the page.
- An attempt to run six parallel research tracks failed before producing anything, so some
  planned depth was dropped: open-source scheduler issue trackers, detailed vendor terms, the
  TikTok Business API, auth and queue library comparisons. Those areas are marked below.
- Nothing was tested against a live account. No developer app was created.
- Where a file says [Unverified] or "working knowledge", it means I did not re-read a source in
  this pass.

## Questions for the owner

| # | Question | Why it matters | My assumption until told otherwise |
| --- | --- | --- | --- |
| Q1 | What exactly does "dual use" mean to you? | It shapes spec 01 | Runway keeps serving Muse and also works standalone for people, later for other creators. A second channel of your own would be a second workspace and is covered. |
| Q2 | How does Muse talk to Runway today: HTTP routes, the CLI, or in-process? Which routes? | Spec 06 must not break it, and the "require a key for writes" switch needs an exemption list | Unknown. Every existing route is left unchanged. |
| Q3 | For TikTok before any audit: inbox drafts, a vendor, or stay manual? | Decides work package 3.7 and whether any sign-up is needed | Try inbox drafts (test T-2); otherwise manual. No vendor without your approval. |
| Q4 | Is the hosted product a real goal now, or should effort stop at "works for my channels"? | P7–P9 are large and bring ongoing duties (reviews, policies, support) | Build P0–P6 first; decide on P7+ after the owner's own publishing works. |
| Q5 | Would you register a business if Meta requires it for other people's Instagram accounts? | Blocks Instagram in the hosted product | Unknown; find out before starting Meta's review. |
| Q6 | May Runway sign up for free services on your behalf (Cloudflare R2 bucket, tunnel, identity provider)? | Each is free at the sizes involved but is still an account and terms | No. Each is listed in spec 09 as your action. |
| Q7 | Which machine is the "home" of your instance, and do you need a second one? | Spec 07's lease design | One primary machine; occasional handover. |
| Q8 | Should Muse-generated posts carry the platforms' AI-content label by default? | Spec 06 defaults it on | Yes, with a human able to change it. |
| Q9 | Does the repository stay public long term? | Spec 07 exists because it is | Yes. |

## Facts I could not establish

### Would change the plan

| # | Unknown | Where it bites | How to resolve |
| --- | --- | --- | --- |
| U1 | Does the 2026-11-02 YouTube change really free a new unaudited project from the private lock? | The YouTube-first recommendation | Test Y-3 |
| U2 | Do TikTok inbox uploads work from a sandbox or unaudited app? | TikTok for the owner | Test T-2 |
| U3 | Can a draft-only or early-stage app pass TikTok app review at all? | TikTok in hosted mode before the audit | Ask TikTok developer support, or submit and see |
| U4 | Can a solo developer without a company pass Meta Business Verification? | Instagram in hosted mode | Read Meta's Business Help Center document list; try the flow up to the document step |
| U5 | Does Instagram fetch from R2 signed URLs? | Spec 02's Instagram delivery | Test I-2; fallback documented in the plan |
| U6 | Does "In production, unverified" avoid 7-day Google tokens? | Owner's YouTube connection staying alive | Test Y-1 |

### Would change details

- TikTok token lifetimes. The page read on 2026-11-14 gave 2 hours and 180 days; older material
  I recall gave 24 hours and 365 days. The spec reads lifetimes from responses, so either is
  handled.
- Instagram's real daily publishing limit (the page says both 100 and 50).
- Instagram Login token lifetime and refresh rules (page did not render).
- Whether Instagram media can be edited or deleted by API.
- Current Instagram insight metric names.
- YouTube WebSub lease length and signature behaviour.
- Whether custom thumbnails need a phone-verified channel.
- The Shorts length limit as applied to API uploads.
- TikTok webhook retry policy; TikTok PKCE and redirect rules for web.
- Whether the US TikTok joint venture affects developers. I found no documented split.
- Whether YouTube scopes are formally "sensitive" (not "restricted"). The Cloud Console labels
  each scope.
- Current terms of Cloudflare Tunnel, Supabase, Render and Oracle free tiers.
- The current state of the Model Context Protocol's transport and authorization sections.

### Not researched in this pass

- Open-source schedulers' issue trackers (Postiz, Mixpost) for real-world failure patterns.
- Vendor terms of service, upload limits, reliability and whether TikTok posts are truly public.
- The TikTok API for Business (comments and richer insights).
- A proper comparison of self-hosted auth libraries and Postgres queue libraries.
- Privacy law specifics (PIPEDA, GDPR, US state laws). Spec 05 lists what the platforms require
  and says to get legal review.
- Pricing, billing and market questions. `WHAT_IS_NEXT.md` covers the business side and argues
  against this direction; that argument stands unanswered here.

## Things that surprised me and should be watched

1. Platform rules changed three times in the four weeks between the earlier research and this
   one. Anything here older than about 60 days should be re-read before building on it.
2. YouTube now defaults an omitted privacy value to `public`. Code written from older examples
   that omit it would publish publicly.
3. TikTok's personal-use rule means the owner's own TikTok can never post publicly through
   Runway's own app unless Runway is a real public product.
4. Meta's Unified Login makes Runway's current Instagram integration the older of two paths,
   four weeks after it was written.
