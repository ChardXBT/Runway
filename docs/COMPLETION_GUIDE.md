# LeeWay completion guide

LeeWay's software build is complete: real Qlob history, retrieval, discovery, question-first
captions, feedback learning, review, internal scheduling, and a guarded visible-browser YouTube
scheduler are implemented. The only remaining deployment acceptance is one explicitly authorized
real scheduling pass. It is intentionally excluded from routine tests because it creates an
external post.

## What each stage needs

| Stage | LeeWay needs | User/account input | Status |
| --- | --- | --- | --- |
| Historical catalogue | Signed-in visible browser, Qlob Posts URL, local disk | Qlob Editor account; manual Google challenges | Verified: 771 posts, 716 media files |
| Qlob retrieval profile | Historical images/captions, Codex included usage | ChatGPT sign-in for Codex; no API key | Verified: 698 annotations |
| Image discovery | Search surface, local storage, provenance URLs | Manual challenge handling and source judgment | Bounded live pass verified |
| Caption generation | Candidate image, bounded history, style profile, feedback memory | Review decisions over time | Implemented and tested |
| Review/approval | Caption choices, image preview, source record | Edit/select/reject/approve and provenance decision | Implemented and tested |
| Internal schedule | Approved proposal and Toronto date/time | Choose the intended queue slot | Implemented and tested |
| External schedule | Dedicated publisher profile, feature gate, approved internally scheduled proposal | Qlob Editor sign-in and exact final confirmation | Implemented; real acceptance pending |

## 1. Model adaptation now

LeeWay does not retrain Codex weights. It adapts locally and immediately:

1. The historical Qlob database defines channel length, tone, structures, visual patterns, and
   rotation evidence.
2. Every caption pass receives at most eight relevant visual examples and eight style examples,
   rather than the entire catalogue.
3. The model proposes nine candidates: four open questions, three observations, and two reactions.
4. LeeWay injects a grounded visible-emotion question when character and emotion confidence are
   high—for example, `Why is Homer so excited?`.
5. A deterministic reranker rewards open-ended `why`/`how`/`what` questions, Qlob style, suitable
   length, novelty, and similarity to accepted edits. It penalizes yes/no questions, generic bait,
   flat descriptions, repetition, and similarity to rejected captions.
6. The review UI stores edits, selected alternatives, approvals, explicit preferences, rejections,
   reason codes, image verdicts, and notes in append-only feedback records.
7. Those records enter the next bounded retrieval package immediately. No paid API, GPU, or
   fine-tuning job is required.

Training quality will improve as the operator consistently records why a caption worked or failed.
The strongest feedback is a specific edited caption plus a reason such as
`prefer_open_question`, `too_generic`, `wrong_emotion`, or `invented_context`.

## 2. Review and provenance

For each proposal:

1. Compare the original and square preview.
2. Verify detected character/emotion metadata; correct it before regenerating if wrong.
3. Prefer a specific open-ended question grounded in what is visible.
4. Save the final caption and useful feedback reasons.
5. Open the source page. For an `unknown` source, record a proposal-specific provenance decision.
   This records human review; it does not claim that the image is licensed.
6. Approve or reject. Approved content becomes immutable once internally scheduled.

Likely fan art, personal artwork, commissions, portfolios, unsafe images, prominent watermarks, and
duplicates are filtered before proposal generation. Human provenance review remains mandatory for
unknown internet sources.

## 3. Internal scheduling

Approval and scheduling are separate:

```text
needs_review -> approved -> internally_scheduled
```

After `internally_scheduled`, the image, caption, metadata, and date cannot be edited through the
editorial APIs. This prevents the prepared external payload from drifting after approval.

## 4. Guarded external scheduler

The publisher is disabled by default. Setup:

```powershell
.\.venv\Scripts\leeway.exe publisher login
```

Sign into the Google account that YouTube identifies as an Editor for Qlob. The dedicated profile
is retained locally until Google expires or challenges the session.

For a controlled acceptance only, set:

```dotenv
LEWAY_PUBLISHING_ENABLED=true
LEWAY_PUBLISHER_CHANNEL_ID=UCQ-nHijGwxNU3Go_wyLQ5Ng
LEWAY_PUBLISHER_CONFIRMATION_TTL_MINUTES=10
```

Restart LeeWay, then validate:

```powershell
.\.venv\Scripts\leeway.exe publisher status
```

Preparation is safe and does not submit:

```powershell
.\.venv\Scripts\leeway.exe publisher prepare --proposal-id <ID>
```

It returns a one-time token and exact phrase. Before confirming, inspect:

- Qlob channel identity;
- proposal ID and internally scheduled state;
- exact local image and final caption;
- Toronto date/time and whether it is safely in the future;
- source/provenance decision; and
- Manager recovery contact.

Confirmation creates the external side effect:

```powershell
.\.venv\Scripts\leeway.exe publisher confirm --attempt-id <ATTEMPT_ID>
```

Enter the hidden token and exact phrase only after separately authorizing that exact proposal.
LeeWay then opens a visible browser, fills the Qlob composer, clicks Schedule once, captures
evidence, and checks the Scheduled tab.

If verification is inconclusive, do not submit again:

```powershell
.\.venv\Scripts\leeway.exe publisher verify --proposal-id <ID>
```

After acceptance, return `LEWAY_PUBLISHING_ENABLED=false` and restart the services.

## 5. Completion definition

Software-complete criteria are met when:

- migrations apply from an empty database and preserve the production database;
- offline tests, type checks, lint, formatting, frontend tests, build, and audits pass;
- question-first feedback learning is visible and persisted;
- no model or background task can call the external publisher;
- preparation performs no submission;
- one-time confirmation, tamper checks, ambiguous recovery, and verification tests pass; and
- tested code is pushed to private GitHub `main`.

Deployment acceptance is complete only after the user separately authorizes one controlled real
schedule and the exact post appears in Qlob's Scheduled tab. Until then, do not describe the
current account-specific YouTube integration as externally proven.
