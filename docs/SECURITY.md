# Security

- Services bind to loopback by default.
- `.env`, databases, media, snapshots, and browser profiles are ignored by Git.
- No Google password or owner credential is requested or stored.
- Live browser adapters are headed, explicit, challenge-aware, and contain no stealth/evasion.
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
- Publishing is disabled; the internal publisher requires an already approved proposal.
