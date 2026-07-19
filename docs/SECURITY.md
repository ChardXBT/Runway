# Security

- Services bind to loopback by default.
- `.env`, databases, media, snapshots, and browser profiles are ignored by Git.
- No Google password or owner credential is requested or stored.
- Live browser adapters are headed, explicit, challenge-aware, and contain no stealth/evasion.
- The local browser-agent capture bridge accepts only bounded HTTPS YouTube DOM checkpoints,
  requires an explicit source header, and validates exact count/tail agreement before completion.
  It has no publishing capability.
- External images retain provenance and default to unknown rights status.
- Search planning excludes fan art, personal artwork, portfolios, commissions, and independent
  illustrations. Model-detected fan art or personal artwork is warned at probability `> 0.25` and
  hard-rejected at probability `>= 0.50`.
- Codex must be authenticated through `Sign in with ChatGPT`. API-key authentication is rejected.
- Codex subprocesses remove `OPENAI_API_KEY`, `CODEX_API_KEY`, and `CODEX_ACCESS_TOKEN`, run
  ephemerally in an isolated temporary directory, use a read-only sandbox, never request
  approvals, and have web search disabled.
- Model jobs are serialized to avoid accidental parallel allowance consumption. Authentication,
  usage-limit, timeout, and malformed-output failures stop the batch; no paid API fallback exists.
- Model output is schema validated, treated as data, and never executed as SQL or a command.
- External publishing is feature-gated. The guarded publisher requires the operator's explicit
  `Accept` action, internal scheduling, a future timezone-aware daily slot, a valid
  Qlob Editor session, and an unchanged payload hash. The normal UI has no rights declaration or
  typed-phrase step; an explicitly blocked candidate still cannot be scheduled.
- Google sign-in occurs manually in ordinary installed Chrome with an isolated
  Runway profile. Playwright is not active during credential entry. Runway does
  not bypass Google warnings, reduce account protections, export cookies, or
  automate passwords or multifactor challenges.
- Outbox attempts are persisted and serialized. A stale login pauses before composer interaction;
  resume is explicit after sign-in. Google credentials and cookies remain inside the ignored
  dedicated browser profile.
- A final Schedule click is marked possibly submitted before it is attempted. Ambiguous failures
  enter a verification-only state, preventing automatic duplicate retries.
- Editorial mutation APIs reject image/caption/date/metadata changes after internal scheduling.
