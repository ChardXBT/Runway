# Connecting personal channels while the code repository is public

Question from the owner: Runway's repository is public. How can the owner connect personal
channels and keep that instance in sync, "maybe git encryption or a fork"?

Read 2026-11-14.

## 1. What actually needs to be kept and synced

| Data | Size | Sensitivity | Changes |
| --- | --- | --- | --- |
| Instance settings (`.env`: channel names, modes, platform client ids and secrets) | tiny | **Secret** (client secrets) | rarely |
| Platform tokens (`data/secrets/platform-tokens.json`, already Fernet-encrypted) and its key (`data/secrets/token.key`) | tiny | **Secret**: grants posting on the owner's channels | on every refresh |
| Database (`data/qlob-production/runway.db`) | about 470 MB | Private: comments, analytics, unpublished drafts, training data | constantly |
| Media (captured history, candidates, drafts, screenshots) | large | Private; some third-party copyright | often |
| Browser profile for the Community-post publisher | large | **Secret**: a signed-in Google session | on use |

Everything in this table is already ignored by git (`.gitignore`: `.env`, `data/*`, `*.db`).
`AGENTS.md` already forbids adding any of it to the public repository or a public release. So
the code repository is safe today; the gap is that this data exists on one machine only, with no
versioned copy and no way to bring a second machine up.

## 2. Facts that rule options in or out

- [Official] GitHub: "You cannot change the visibility of a fork by itself." A fork of a public
  repository is public. A private copy must be a separate repository (a "mirror"), not a fork.
  <https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/about-permissions-and-visibility-of-forks>
- [Official] GitHub blocks files over 100 MiB in git and recommends repositories stay under
  1 GB. The database is about 470 MB, so it cannot be an ordinary tracked file. Release assets
  have no stated total or bandwidth limit.
  <https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github>
- [Official] git-crypt's documented limits: file names, commit messages and file sizes are not
  encrypted; access cannot be revoked ("Anyone with an old key can still read past history");
  encrypted files do not delta-compress, so each change stores the whole file; an attacker who
  can modify the repository can disable encryption by editing `.gitattributes`; it is designed
  for a mostly public repository with a few secret files.
  <https://github.com/AGWA/git-crypt/blob/master/README.md>
- [Repo] `scripts/sync_private_database_release.ps1` already packages the database and media as
  GitHub release assets, with a manifest and hashes, and refuses to run while
  `ChardXBT/Runway` is public.

## 3. Options

| Option | How it works | For | Against |
| --- | --- | --- | --- |
| A. git-crypt (or SOPS) inside the public repo | Encrypted files committed next to the code | One repository; familiar from the owner's other repos | Ciphertext of live channel tokens is public **forever**: git history cannot be recalled from clones and forks. If the key ever leaks, every past token and database snapshot is readable. Access cannot be revoked. File names and change frequency are visible to everyone. Tokens change on every refresh, so the history grows with noise. The database is too large for git anyway. CI and review bots see ciphertext churn. |
| B. Private mirror of the code ("private fork") | A second, private repository with the same code plus the data | Feels simple | Two copies of the code that drift. Every change must be pushed twice or merged across. The public repo stops being the real one. Still cannot hold the database as a git file. |
| C. Make the repository private | Flip visibility | Simplest; the release script then works as written | The repository is a public showcase linked from the owner's website. Public forks stay public. Does not by itself give a sync workflow. |
| **D. Private companion repository** | Code stays public. A separate private repository holds this instance's settings and encrypted secrets as files, and database and media snapshots as release assets. The app reads its instance directory from one setting. | Clean split: public code, private state. The existing release script needs only a different target repository. Access is revocable (it is a private repo with normal permissions). No ciphertext is ever public. Works today, before any hosting. | A second repository to remember. Snapshots are point-in-time, not live. |
| E. Hosted Runway | Once hosted mode exists, the owner's workspace lives in the hosted database and object store | Real multi-device access with no sync step | Does not exist yet; the intelligence engine and browser publisher are local-only by design. |

## 4. Recommendation

**Option D now, converging on E for the platform data later.**

Why D is the best available choice:

1. **It is the only option where nothing secret is ever published, even encrypted.** With A, the
   safety of the owner's channels would depend forever on one key never leaking, and a mistake
   could not be undone.
2. **It fits the size.** Release assets carry the 470 MB database; git files cannot.
3. **It reuses what exists.** The release script, its manifest and its "refuse if public" guard
   are already written and reviewed.
4. **No code drift.** Unlike B, there is one copy of the code.
5. **It keeps the showcase.** Unlike C.

Encryption still has a role, inside the private repository: secrets stored there should be
encrypted with `age` (via SOPS) so that a leaked repository token or an over-broad collaborator
grant does not expose live channel tokens. That is defence in depth on a private repo, which is
a very different risk from publishing ciphertext to the world.

What should **not** sync at all:

- The publisher's browser profile. It is a signed-in Google session tied to a machine; copying
  it between machines is a way to trigger Google's account-security checks. Sign in again on
  each machine.
- `token.key` in plain form. It goes in the private repository only as an `age`-encrypted file,
  or stays in a password manager.

One rule the owner must follow: **one active machine at a time.** TikTok rotates refresh tokens
on every refresh, so two machines using the same token set will invalidate each other and force
a reconnect. Google and Instagram tolerate it better, but the database is also single-writer.
The design in [`../specs/07-private-instance-data.md`](../specs/07-private-instance-data.md)
enforces this with a lease file.

## 5. Could not determine

- Whether the owner's GitHub plan limits private-repository release storage in practice. The
  documentation states no limit for release assets.
- Whether any automation the owner runs (code-review bots, the Supervisor's auto-update) would
  touch a new private repository. They should be told to ignore it.
