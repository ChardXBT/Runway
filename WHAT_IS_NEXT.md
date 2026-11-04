# Runway Startup Expansion

A summary of the "Evaluate creator startup direction" planning session (Codex, 2026-10-19),
followed by Claude's own take on direction. Written 2026-10-29.

The starting question: Runway works, but is not itself a good startup. What adjacent idea has
real revenue and a business behind it, and how much of Runway carries over?

---

## Part 1 — Summary of the session

### The original idea

"Media: making you the brightest star." Content is growing (UGC and large brands alike), ads no
longer need to look like ads, and trend/stat awareness is what wins. The sketch was:

- **Connections** — MCP/API connectors to every social account, with easy onboarding.
- **Intelligence** — a trends and analytics layer that understands the content itself.
- **Operations** — a content manager (likes, comments, what a client like Muse is doing on
  Qlob) and a multi-platform scheduler/poster.
- An "OpenCode-style" product that combines existing tech rather than reinventing it.

### Runway as described in the original notes

Runway was already built as a database intelligence engine. The startup question was whether that
work could support a larger media business. The original product outline was:

- **Generator:** discover an image, generate a primary caption and alternatives, then let a person
  reject, edit, or accept. Optional actions were to regenerate the image or regenerate the captions.
- **Scheduler / Lineup:** organize accepted posts in a calendar and upcoming list; show image and
  caption previews, scheduled time, and YouTube sync status; move a post, swap two posts, edit a
  scheduled caption, remove a post, and retry or verify failed YouTube actions.
- **Supporting sections:** Archive for historical posted and possibly rejected posts, Activity
  History, and Settings for platform connections.
- **YouTube setup idea:** the channel owner would invite Runway as an Editor in YouTube Studio.
  This is not the same as authorizing the YouTube APIs. Channel permissions do not grant API
  access to a delegate; API data access needs the channel owner’s authorized OAuth connection.
- **Data question:** collect enough useful history quickly to power the database intelligence
  layer. The later recommendation is to start with one customer's linked briefs, assets, results,
  revisions, and decisions, with consent, rather than gather unrelated social data.
- **Account setup idea:** use a dedicated, detailed email address for future platform accounts.
  No address, domain, or account-ownership policy was selected in the conversation.

These bullets record the initial proposal. They do not claim that each item was implemented in
Runway. The current product behavior and platform status are documented in the README and the
platform capability documents.

### Core finding: the idea is four businesses at once

| Layer | What it means | The question it has to answer |
|---|---|---|
| Connections | Pull data from platforms via APIs/MCP | Can we get the data reliably and affordably? |
| Operations | Scheduling, publishing, comments, approvals, archive | Which recurring task is painful enough to pay for? |
| Intelligence | Trends, content analysis, recommendations, brand memory | Which decision gets better because of us? |
| Outcome | Growth, sales, production capacity | Does the customer see enough value to keep paying? |

Covering all four is why the idea feels so big. "MCP to connect everything" is an implementation
detail and "brightest star" is the ambition; what is missing is **one precise buying reason** —
one buyer, one recurring task, one measurable benefit.

### The market is already crowded at every layer

- **General social management:** Buffer (publishing + analytics + AI + approvals, ~$5/channel/mo),
  Hootsuite, Metricool. A shared dashboard competes on price with these.
- **Connector infrastructure:** Ayrshare (publishing, analytics, MCP; $299/mo for 10 profiles) and
  Phyllo (normalized creator data). This is probably the "big channels connector company" that
  was half-remembered. The connection layer can be bought, not built.
- **Creative intelligence:** Motion's Runneth (briefs, hooks, brand memory; from $750/mo),
  Foreplay (research → analytics → briefs + MCP; $175/mo), Billo (results → UGC briefs),
  Dash Social (brand-specific creative analysis). "AI that understands your content" is
  **not** a differentiator on its own.
- **Creator-program ops:** Shortimize (tracking, payments, API/MCP; from $99/mo).

The market thesis is real (IAB: ~$37B US creator ad spend in 2025, +26% YoY, with measurement and
ops tooling called out as weak spots), but that is ad spend, not proof of demand for a product.

### Ranked starting points

| Starting area | Assessment |
|---|---|
| **Production feedback for small creative / UGC agencies** | **Best first test** |
| Creator-program operations for one business type | Alternative if interviews expose a specific failure |
| Editorial assistant for professional channel teams | Closest to what Runway already is |
| General creator operating system | Weak — broad, cheap competitors, many integrations |
| Universal trend / virality predictor | Poor first promise — hard to prove |

### The recommended hypothesis

> **"We help small creative agencies use past results to decide what to produce next, while
> preserving each client's creative history."**

Target: boutique agencies making recurring short-form ads for e-commerce brands; buyer is the
owner or head of creative. Their pain: *"We deliver content, but collecting client results and
turning them into the next shoot plan takes too much work."*

The loop the product owns: **brief → asset → published variation → results → decision → next brief.**

A first deliverable per cycle: which concepts earned another iteration, which flopped or lack
data, three proposed tests, briefs in the client's format, and a client-ready explanation linked
to evidence. Possible differentiation (unverified): tracing one concept through its many edits and
ads across client accounts, and fitting the agency's approval/reporting process.

The decisive interview question: *"Why can't you do this with Motion, Foreplay, Billo, or your
current process?"* If the answer is "yours looks nicer," the case is weak.

### What Runway contributes

Reusable: content-history retrieval, grounded caption generation, the editorial review desk
(reject / edit / accept), and stored accept/reject feedback. Not reusable as the product driver:
the calendar/scheduler.

Keep two learning signals separate: **editorial preference** (what the user accepted) is not
**business performance** (what happened after publishing). An accepted caption shows taste, not
causation.

### Platform reality checks

1. Adding Runway as a YouTube Studio editor **does not grant API access** — that needs a proper
   OAuth flow.
2. TikTok's Display API exposes limited profile/video data; Direct Post is separate, and unaudited
   clients are restricted to private posting.
3. YouTube restricts derived/combined metrics across platforms, so a universal engagement score
   needs care.
4. MCP exposes tools to an agent; it does not expand platform permissions.

So the pilot should run on **customer-provided assets and standard performance exports**, not
live connectors. Faster database growth comes from one customer's linked production history
(briefs, assets, results, revisions, decisions), not from scraping many unrelated posts.

### MVP scope (sellable by hand)

1. Import assets and performance exports for one client.
2. Link asset variations back to their original brief.
3. Tag creative attributes, with human correction.
4. Produce an evidence-backed review and next tests — framed as hypotheses, with caveats such as
   unequal ad budgets.
5. Record which recommendations were used and what happened next.

Deferred: broad publishing, a universal inbox, five-platform onboarding.

### Pricing (untested proposals)

- **$300–$750 paid pilot** covering one client account and two production reviews.
- Move to a subscription only after the agency uses the output in real work.
- Sanity math: 8 h/month saved × $50/h = $400 of labor value, so $750/mo needs more than time
  savings. Watch connector costs (Ayrshare counts each brand as a profile).

### The 30-day plan

| Week | Work | Evidence to get |
|---|---|---|
| 1 | List 30 relevant agencies; interview 8–10 | Real recent examples, current tools, time spent, who holds budget |
| 2 | Hand-produce a review from a consenting agency's assets/exports | Did it change an actual production decision? |
| 3 | Pitch three paid pilots with identical scope | Willingness to pay for the same recurring task |
| 4 | Deliver a second cycle; discuss continuation | Repeat use, time saved, data friction, renewal intent |

Interview prompts: "Show me the last time you decided what to produce next." · "How did results
get back to whoever wrote the brief?" · "Which part took longest?" · "What did your tools fail to
do?" · "Who can approve paying for this?"

**Continue if:** three paid pilots, repeated use of the recommendations, and at least two
customers who want to keep going. **Change the hypothesis if** agencies can't get client results,
don't own this task, or already solve it with existing tools.

Note: when asked, there was **no clear access to customers yet** — so the plan is as much a
customer-discovery exercise as a product plan.

---

## Part 2 — Claude's take on direction

*Opinion, not research. Weigh it accordingly.*

**I agree with the core recommendation, and I'd push it harder.** The session's best insight is
that the value isn't in connecting accounts or generating content — it's in the *decision*
"what do we make next?". Everything else is commodity. Build nothing that isn't on the path from
"here are last cycle's results" to "here is next cycle's brief."

**The biggest risk is not competition, it's customer access.** You said you have no clear way to
reach buyers yet. That makes Week 1 the whole game. Before writing any new code, I would spend a
week doing only outreach: find agencies through public portfolios, TikTok/IG "UGC agency" bios,
Upwork/Contra listings, and founder communities, and aim for 10 conversations. If you can't get
10 conversations in two weeks, that is itself a signal to pick a buyer you can actually reach —
which might be the "channel teams" option, since that's closer to people you already know
through Runway/Muse.

**Treat Runway as a toolbox, and stop expanding it.** The most recent commit expanded Runway
into a multi-platform creator desk (YouTube, Instagram, TikTok). That's the "general creator OS"
the session ranked weakest. I'd freeze feature work on Runway's scheduler/publisher and only
extract what the pilot needs: the review desk UI (accept/edit/reject maps directly to "accept
this recommendation"), the retrieval/evidence layer, and the feedback store. The pilot's first
"product" can honestly be a spreadsheet plus a generated PDF review — Runway's pieces matter in
month two, not week one.

**Your real edge might be speed of iteration, not features.** Motion at $750/mo and Foreplay at
$175/mo are built for in-house brand teams and larger agencies. A boutique agency with 5–15
clients may find them too expensive or too generic. A done-with-you service at a few hundred
dollars per client per month, where you personally run the analysis with your own tooling, is a
legitimate way to start — and every manual review teaches you what to automate.

**Be careful about the data story.** The thing that would make this defensible over time is the
linked history (brief → variants → results → decisions) per client. That is only valuable if
it's clean and connected. Design the data model around that chain from day one, even if the UI
is crude — Runway's `DATA_MODEL.md` and intelligence flywheel docs are a good starting point to
adapt rather than rewrite.

**Concrete next steps I'd take this week:**

1. Write the one-paragraph pitch and a one-page sample review (use a public brand's ads as a
   mock example) so you have something to show in outreach.
2. Build a list of 30 boutique UGC/short-form agencies with a named contact for each.
3. Send outreach; book the first 3–5 calls.
4. In parallel, timebox (≤2 days) a minimal import → tag → review pipeline reusing Runway's
   retrieval and review code, only enough to produce the Week 2 manual review faster.
5. Revisit this file after Week 2 with what the interviews actually said, and be willing to
   change the buyer.

One caution: this repository is **public**. If customer names, pricing conversations, or pilot
data start accumulating, move this planning into a private repo or doc.

---

## Completeness note

Reviewed against the startup notes, the linked Runway conversation, the follow-up that there is no
clear customer access yet, and the research summary in this chat. The original media thesis,
four-layer framing, known competitors, connector/MCP idea, Runway workflow and setup ideas,
platform access limits, candidate wedge, unvalidated pricing, customer interview plan, and open
risks are captured above. This is an organized expansion of the discussion, not a verbatim chat
transcript. Product claims about the current repository should be checked against its linked
product and capability documentation.

Related implementation and research detail already lives in this repository:

- [`README.md`](README.md) and [`docs/PRODUCT.md`](docs/PRODUCT.md) describe the current creator
  desk and Runway's implemented product behavior.
- [`docs/CREATOR_OS_SETUP.md`](docs/CREATOR_OS_SETUP.md),
  [`docs/PLATFORM_RESEARCH.md`](docs/PLATFORM_RESEARCH.md), and
  [`docs/PLATFORM_CAPABILITIES.md`](docs/PLATFORM_CAPABILITIES.md) hold setup, competitor/API
  research, and platform-specific status.
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md),
  [`docs/INTELLIGENCE_DATA_FLYWHEEL.md`](docs/INTELLIGENCE_DATA_FLYWHEEL.md), and
  [`docs/INTELLIGENCE_DATA_FLYWHEEL_REPORT.md`](docs/INTELLIGENCE_DATA_FLYWHEEL_REPORT.md) cover
  the database and learning-loop work behind Runway.

Last checked against the conversation: 2026-11-04.
