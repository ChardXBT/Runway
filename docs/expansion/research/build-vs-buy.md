# Build or buy the publishing layer

Read 2026-11-14. Vendor prices are list prices from the vendors' own pages and change often.
Vendor claims about themselves are marketing until tested.

## 1. What "buy" means

Unified social API vendors hold the platform approvals (TikTok audit, Meta advanced access,
Google verification). Your product calls their API; the post goes out through their approved
platform apps. Your users connect accounts through the vendor's consent flow.

## 2. Vendors

| Vendor | Free tier | Paid entry | Billing unit | Notes | Source |
| --- | --- | --- | --- | --- | --- |
| Zernio (formerly Late / getlate.dev) | First 2 connected accounts free, "every feature included" | $6 per account for accounts 3–10, $3 for 11–100, $1 beyond | Connected account | Publishing, analytics, comments, webhooks, hosted MCP server. Based in Spain; claims SOC 2 Type II. | <https://zernio.com/pricing> |
| Upload-Post | 10 uploads per month, 2 profiles, **no TikTok** | $24/mo for 5 profiles, unlimited uploads; $50/mo for 25 | Profile (one account per platform) | White-label connect from $50/mo. | <https://www.upload-post.com/pricing/> |
| bundle.social | 20 posts per month, 3 social accounts | $100/mo for 10,000 posts, unlimited accounts | Posts | Analytics on paid plans. | <https://bundle.social/pricing> |
| Post for Me | None found | $10/mo for 1,000 posts, unlimited accounts | Posts | Lets you use their platform credentials **or bring your own**. | <https://www.postforme.dev/pricing> |
| Ayrshare | None; 28-day trial on Launch only | $149/mo for 1 profile; $299/mo for 10 | Profile | The incumbent. Most expensive. | <https://www.ayrshare.com/pricing/> |
| Outstand | Not read (pricing page 404) | Usage-based, bring-your-own-keys | — | — | <https://www.outstand.so> |

Cost at three sizes, three platforms per workspace [Inference from the tables above]:

| Workspaces | Accounts | Zernio | Upload-Post | bundle.social | Post for Me |
| --- | --- | --- | --- | --- | --- |
| 1 (owner) | 3 | $6 (2 free + 1) | $24 (free tier lacks TikTok) | $0 if ≤ 20 posts/month | $10 |
| 10 | 30 | $108 | $50 | $100 | $10 if ≤ 1,000 posts |
| 100 | 300 | $518 | $438 | $100 if ≤ 10,000 posts | volume plan |

Not verified for any vendor: that TikTok posts really go out public; upload size limits; rate
limits; what happens to connected accounts if you leave. Tokens held by a vendor cannot be
exported, so leaving a vendor means every user reconnects.

## 3. Risks of buying

- **Dependence.** The vendor's platform approval is the product. If a platform revokes the
  vendor's app, every customer's posting stops the same day.
- **Custody.** The vendor holds your users' platform tokens. Your privacy policy must name them
  as a sub-processor.
- **Policy grey area.** [Unverified] Platform terms are written for an app serving its own
  users. Whether an approved app may be resold as infrastructure to other products is not
  something I could confirm from platform terms. The vendors exist openly and at scale, which is
  evidence of tolerance, not permission.
- **Margin.** Per-account pricing is a permanent cost of goods.
- **Less data.** Runway's analytics, inbox and sync already work directly. A vendor would only
  be used for publishing, so two integrations exist per platform either way.

## 4. Risks of building

- Three approval processes, each able to say no: Google verification (stated up to 10 days),
  Meta App Review plus Business Verification, TikTok audit.
- TikTok will not approve until the product is public.
- Ongoing maintenance as platform rules move (three material changes in the four weeks between
  the earlier research and this one).

## 5. Recommendation: build direct, keep a vendor adapter as a bridge

Build the direct adapters, and define the `Publisher` port (see
[`../specs/03-publishing-engine.md`](../specs/03-publishing-engine.md)) so a vendor adapter can
sit beside them.

| Platform | Owner's own accounts, now | Hosted product, later |
| --- | --- | --- |
| YouTube | Direct. No audit needed since 2026-11-02; quota is ample for one channel. | Direct, after Google verification. |
| Instagram | Direct. Standard Access is enough. | Direct, after App Review and Business Verification. |
| TikTok | Inbox drafts through Runway's own sandbox app if the test passes; otherwise a vendor adapter or the manual workflow. | Direct after the audit; vendor adapter until then. |

Why this is the best choice:

1. Two of three platforms need no approval for the owner's use, so buying would add cost and a
   dependency for nothing.
2. Runway already has direct read integrations; publishing reuses the same tokens, accounts and
   error handling.
3. The only platform where buying beats building is TikTok, and only until the audit passes.
   A port makes that a configuration choice per account, not an architecture choice.
4. The owner's rule is to spend nothing without approval. Direct integration costs $0. The
   cheapest working vendor path for TikTok is Zernio's free tier (2 accounts), which needs an
   account sign-up the owner must approve.

Strongest counter-argument: a solo developer could ship hosted publishing for all platforms in
days through a vendor instead of months, and find out whether anyone wants the product before
doing three approval processes. If the hosted product becomes the priority over the owner's own
channels, start with the vendor adapter for all three platforms and replace it platform by
platform. The port makes that possible without rework.

## 6. Open-source references

Earlier research noted Postiz (AGPL-3.0; NestJS, Prisma, Postgres, Temporal, Redis) and Mixpost
(Laravel). I did not re-read their code or issues in this pass. Their public documentation says
self-hosters must create and get approved their own platform apps, which matches the platform
findings here. **Do not copy Postiz code**: AGPL would bind Runway.

## 7. Could not determine

- Whether each vendor's TikTok posts are public and through which audited app.
- Vendor upload limits, reliability history and terms on reselling.
- Buffer's public API status.
- Whether platforms sanction the reseller pattern.
