# Repository workflow

- Make requested Runway changes directly on `main` unless the user explicitly asks for a branch or pull request.
- Preserve unrelated local changes and untracked files.
- Validate requested changes before committing and pushing them to `origin/main`.
- After `origin/main` is confirmed at the new commit, run
  `powershell -ExecutionPolicy Bypass -File scripts/sync_private_database_release.ps1 -TargetSha <full-sha>`.
  This post-push order is mandatory: never publish the private database release before GitHub has
  accepted the commit recorded in its manifest.
