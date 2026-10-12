# Repository workflow

- Make requested Runway changes directly on `main` unless the user explicitly asks for a branch or pull request.
- Preserve unrelated local changes and untracked files.
- Validate requested changes before committing and pushing them to `origin/main`.
- After pushing, confirm `origin/main` is at the new commit and check the GitHub Actions run for it.

# Production data

- `ChardXBT/Runway` is a public repository. Do not run
  `scripts/sync_private_database_release.ps1` as part of normal maintenance; it uploads the
  production database and media to a GitHub release and correctly refuses while the repository
  is public. Never bypass that guard, and never add the database, media, browser profiles,
  authentication state, `.env`, or recovery archives to the repository or any public release.
- Back up the production database locally with SQLite's online backup API, as described in
  `docs/INTELLIGENCE_OPERATIONS.md`, and keep copies outside the repository or in ignored
  `data/` paths.
- The release script remains valid only if the code is moved to a private repository. In that
  case run it strictly after `origin/main` is confirmed at the commit recorded in its manifest:
  `powershell -ExecutionPolicy Bypass -File scripts/sync_private_database_release.ps1 -TargetSha <full-sha>`.
