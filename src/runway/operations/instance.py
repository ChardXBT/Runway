"""``runway instance``: keep this instance's private state in a private companion repository.

Spec 07. The code repository stays public; settings, encrypted secrets, the
token store, the lease and a manifest live in a separate **private** repository,
and database and media snapshots travel as that repository's release assets.

Every external tool (``gh``, ``git``, ``sops``, ``age-keygen``) runs through an
injectable :class:`CommandRunner` with an argument list, no shell and a timeout.
No secret is ever placed on a command line or in an error message.

Git never pushes or fetches by remote name: every transfer names
``https://github.com/<verified owner/name>.git`` explicitly, after the
repository was confirmed private with ``gh`` and after checking that neither
the clone's configured push URLs nor any ``url.*.insteadOf`` rule would send
the data somewhere else.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from runway.config import Settings
from runway.config.instance import (
    CODE_REPOSITORY,
    INSTANCE_TOML_NAME,
    SECRETS_FILE_NAME,
    SOPS_INSTALL_HINT,
    SOPS_TIMEOUT_SECONDS,
    TOKENS_DIR_NAME,
    InstanceConfigError,
    is_code_repository_remote,
    parse_github_remote,
    resolve_instance_dir,
    scrub_tool_output,
)
from runway.locks import FileLockTimeout, exclusive_file_lock
from runway.operations.database_release import (
    DATABASE_ARCHIVE_NAME,
    MANIFEST_NAME,
    MEDIA_ARCHIVE_NAME,
    DatabaseReleaseError,
    create_release,
    sha256_file,
    verify_release,
)
from runway.operations.instance_lease import (
    HEARTBEAT_RENEW_AFTER,
    LEASE_FILE_NAME,
    LEASE_STALE_AFTER,
    Lease,
    LeaseError,
    StandbyError,
    WorkerBusyCheck,
    acquire,
    describe_lease,
    ensure_active,
    ensure_active_or_raise,
    instance_is_active,
    instance_transfer,
    machine_id,
    read_lease,
    record_remote_check,
    release,
    worker_busy_markers,
    worker_is_busy,
    write_lease,
)
from runway.operations.runtime_files import worker_lock_path

__all__ = [
    "CODE_REPOSITORY",
    "SNAPSHOT_TAG",
    "CommandRunner",
    "InstanceContext",
    "InstanceError",
    "LeaseError",
    "LeaseHeartbeat",
    "StandbyError",
    "ensure_active",
    "ensure_active_or_raise",
    "ensure_private_repository",
    "handover",
    "heartbeat",
    "init",
    "instance_is_active",
    "pull",
    "push",
    "read_remote_lease",
    "status",
    "subprocess_runner",
]

logger = logging.getLogger(__name__)

SNAPSHOT_TAG = "snapshot-latest"
DATED_TAG_PREFIX = "snapshot-"
_DATED_TAG_PATTERN = re.compile(r"^snapshot-\d{8}-\d{6}$")
MANIFEST_FILE_NAME = "manifest.json"
MANIFEST_FORMAT_VERSION = 1
SOPS_CONFIG_NAME = ".sops.yaml"
TOKENS_FILE = f"{TOKENS_DIR_NAME}/platform-tokens.json"
ASSET_NAMES = (DATABASE_ARCHIVE_NAME, MEDIA_ARCHIVE_NAME, MANIFEST_NAME)
WORKER_POLL_SECONDS = 5.0
DEFAULT_WORKER_WAIT_SECONDS = 300
HEARTBEAT_THREAD_INTERVAL_SECONDS = 3600.0
# A private ref prefix, so concurrent commands never race on FETCH_HEAD; each
# fetch uses its own ``refs/runway/remote-<uuid4hex>`` ref, deleted in finally.
REMOTE_REF = "refs/runway/remote"
INSTANCE_TMP_DIR = "tmp"
# An OS file lock serializes git-mutating and ref-reading instance operations on
# one clone across the API process, its heartbeat thread and the CLI.
INSTANCE_LOCK_NAME = "instance.lock"
INSTANCE_LOCK_WAIT_SECONDS = 120.0

COMMAND_TIMEOUT_SECONDS = 120
NETWORK_TIMEOUT_SECONDS = 600
TRANSFER_TIMEOUT_SECONDS = 3600
# Git uses gh for HTTPS credentials; any other configured helper is cleared first.
GIT_CREDENTIALS = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")

# The small files committed to the private repository; nothing else is ever added.
TRACKED_FILES = (
    "README.md",
    ".gitignore",
    ".gitattributes",
    SOPS_CONFIG_NAME,
    INSTANCE_TOML_NAME,
    SECRETS_FILE_NAME,
    TOKENS_FILE,
    LEASE_FILE_NAME,
    MANIFEST_FILE_NAME,
)
# Platform credentials moved from the checkout's settings into the encrypted file on init.
MIGRATED_SECRET_SETTINGS = (
    ("youtube_client_id", "RUNWAY_YOUTUBE_CLIENT_ID"),
    ("youtube_client_secret", "RUNWAY_YOUTUBE_CLIENT_SECRET"),
    ("instagram_app_id", "RUNWAY_INSTAGRAM_APP_ID"),
    ("instagram_app_secret", "RUNWAY_INSTAGRAM_APP_SECRET"),
    ("tiktok_client_key", "RUNWAY_TIKTOK_CLIENT_KEY"),
    ("tiktok_client_secret", "RUNWAY_TIKTOK_CLIENT_SECRET"),
)

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_AGE_RECIPIENT_PATTERN = re.compile(r"age1[0-9a-z]{58}")
_SOPS_AGE_LINE = re.compile(r"^(?P<prefix>\s*age:\s*)(?P<value>\S.*?)\s*$", re.MULTILINE)

_INSTALL_HINTS = {
    "gh": "Install the GitHub CLI (winget install --id GitHub.cli) and run 'gh auth login'",
    "git": "Install Git (winget install --id Git.Git)",
    "age-keygen": "Install age (winget install --id FiloSottile.age)",
    "sops": SOPS_INSTALL_HINT,
}

_GITIGNORE = """\
# Plain secrets and databases never enter this repository; snapshots are release assets.
*.db
*.db-wal
*.db-shm
*.db.gz
*.sqlite
*.sqlite3
*.zip
*.key
.env
.env.*
browser-profile/
tmp/
tokens/token.lock
tokens/platform-tokens-*.json
.lease-*.json
.lease-*.tmp
"""
# Byte-exact: the Fernet blob and SOPS ciphertext must never be rewritten for line endings.
_GITATTRIBUTES = "* -text\n"
_README = """\
# Runway private instance

Private state for one Runway installation. Keep this repository **private**.
The code lives in the public `ChardXBT/Runway` repository; nothing here may be copied there.

- `instance.toml`: shared non-secret settings (Runway setting names without `RUNWAY_`).
- `secrets.enc.env`: SOPS + age encrypted secrets. Edit with `sops edit secrets.enc.env`.
- `tokens/platform-tokens.json`: the Fernet-encrypted token store; its key is in the secrets.
- `lease.json`: the one machine allowed to refresh tokens, publish and run the worker.
- `manifest.json`: the last snapshot. The database and media are assets of its dated
  `snapshot-YYYYMMDD-HHMMSS` release, also copied to `snapshot-latest`.

Restore on another machine: clone the code and this repository, install this machine's age
key, set `RUNWAY_INSTANCE_DIR`, then run `runway instance pull` after the active machine ran
`runway instance handover`. See `docs/PRIVATE_INSTANCE.md` in the code repository.
"""
_RELEASE_NOTES = (
    "Private snapshot of this Runway instance: the database (SQLite online backup, "
    "gzip) and every media file it references. Browser profiles, plain keys and "
    "authentication state are never included."
)


class InstanceError(RuntimeError):
    """A ``runway instance`` command refused or failed safely."""


class InstanceBusyError(InstanceError):
    """Another instance operation still holds ``tmp/instance.lock`` on this clone."""


INSTANCE_ERRORS: tuple[type[Exception], ...] = (
    InstanceError,
    InstanceConfigError,
    LeaseError,
    DatabaseReleaseError,
)


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]: ...


# Git config injected through the process environment could redirect a push
# (remote.<url>.pushurl, url.<base>.pushInsteadOf): it never reaches a child.
_GIT_CONFIG_ENV_NAMES = frozenset({"GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})
_GIT_CONFIG_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _isolated_env() -> dict[str, str]:
    """os.environ without injectable git config, plus no-prompt defaults.

    GIT_CONFIG_GLOBAL is kept: the user's credential helper lives there.
    GIT_CONFIG_NOSYSTEM drops the system file, which this process cannot control.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _GIT_CONFIG_ENV_NAMES and not name.startswith(_GIT_CONFIG_ENV_PREFIXES)
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GCM_INTERACTIVE": "never",
            "GH_PROMPT_DISABLED": "1",
        }
    )
    return env


def _git_subcommand(args: Sequence[str]) -> str | None:
    """The first git subcommand after global options and their values."""
    value_options = {
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in value_options:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1  # e.g. --git-dir=..., --work-tree=..., -P, --version
            continue
        return arg
    return None


_GIT_NETWORK_SUBCOMMANDS = frozenset({"push", "fetch", "clone", "ls-remote"})


def _timeout_for(args: Sequence[str]) -> int:
    if args[0] == "sops":
        return SOPS_TIMEOUT_SECONDS
    words = set(args[1:6])
    if args[0] == "gh" and {"release"} <= words and words & {"upload", "download", "create"}:
        return TRANSFER_TIMEOUT_SECONDS
    if args[0] == "gh" and "clone" in words:
        return NETWORK_TIMEOUT_SECONDS
    if args[0] == "git" and _git_subcommand(args[1:]) in _GIT_NETWORK_SUBCOMMANDS:
        return NETWORK_TIMEOUT_SECONDS
    return COMMAND_TIMEOUT_SECONDS


def subprocess_runner(
    args: Sequence[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0])
    if executable is None:
        hint = _INSTALL_HINTS.get(args[0], "Install it")
        raise InstanceError(f"'{args[0]}' was not found on PATH. {hint}.")
    timeout = _timeout_for(args)
    try:
        return subprocess.run(
            [executable, *args[1:]],
            cwd=cwd,
            env=_isolated_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstanceError(f"'{args[0]}' did not finish within {timeout} seconds") from exc


# Used where no InstanceContext exists (the remote lease check); tests replace it.
default_runner: CommandRunner = subprocess_runner


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class InstanceContext:
    """Everything a command touches outside Python, injectable for tests."""

    settings: Settings
    runner: CommandRunner = subprocess_runner
    now: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    worker_busy: WorkerBusyCheck = worker_is_busy

    @property
    def machine(self) -> str:
        return machine_id(self.settings)

    def run(
        self, args: Sequence[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(list(args), cwd=cwd)
        if check and result.returncode != 0:
            raise InstanceError(_failure(args, result))
        return result

    def git(
        self, repository_dir: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.run(["git", "-C", str(repository_dir), *args], check=check)

    def remote_git(
        self, repository_dir: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """A git command that talks to GitHub, authenticated through gh."""
        return self.run(["git", *GIT_CREDENTIALS, "-C", str(repository_dir), *args], check=check)


def _failure(args: Sequence[str], result: subprocess.CompletedProcess[str]) -> str:
    words = [arg for arg in args if not arg.startswith("-") and "=" not in arg]
    shown = " ".join(word for word in words[:3] if not os.path.isabs(word) and "://" not in word)
    return f"'{shown}' failed (exit {result.returncode}): {scrub_tool_output(result.stderr or '')}"


@contextmanager
def _instance_operation(instance_dir: Path, timeout: float | None = None) -> Iterator[None]:
    """Hold this clone's ``tmp/instance.lock`` while an operation mutates it.

    The API heartbeat thread, API request handlers and CLI commands share the
    clone's git state; without a lock an interleaved ``git add``/``commit``/
    ``push``/``reset`` or a deleted remote ref corrupts the other operation.
    Commands wait up to ``INSTANCE_LOCK_WAIT_SECONDS`` and then refuse; callers
    that may not wait (the heartbeat thread) pass ``timeout=0``.
    """
    wait = INSTANCE_LOCK_WAIT_SECONDS if timeout is None else timeout
    lock_path = instance_dir / INSTANCE_TMP_DIR / INSTANCE_LOCK_NAME
    try:
        with exclusive_file_lock(lock_path, timeout=wait):
            yield
    except FileLockTimeout as exc:
        raise InstanceBusyError(
            "another runway instance operation is still running on this clone "
            f"({lock_path}); try again when it finishes"
        ) from exc


# --- repositories -----------------------------------------------------------------------


def validate_repository(name: str) -> str:
    value = name.strip()
    if not _REPOSITORY_PATTERN.fullmatch(value) or value.endswith((".git", ".")):
        raise InstanceError(f"{name!r} is not a GitHub repository name in owner/name form")
    return value


def github_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _code_repositories(ctx: InstanceContext) -> set[str]:
    blocked = {CODE_REPOSITORY.casefold()}
    try:
        origin = ctx.git(
            ctx.settings.project_root, "config", "--get", "remote.origin.url", check=False
        )
    except InstanceError:
        return blocked
    name = parse_github_remote(origin.stdout) if origin.returncode == 0 else None
    if name:
        blocked.add(name.casefold())
    return blocked


def ensure_private_repository(ctx: InstanceContext, repository: str) -> str:
    """Return the canonical name of a confirmed-private repository that is not the code's."""
    name = validate_repository(repository)
    blocked = _code_repositories(ctx)
    code_refusal = (
        "is the Runway code repository; private instance data must go to a separate "
        "private repository (for example <owner>/runway-instance)"
    )
    if name.casefold() in blocked:
        raise InstanceError(f"refused: {name} {code_refusal}")
    result = ctx.run(
        ["gh", "repo", "view", name, "--json", "visibility,nameWithOwner"], check=False
    )
    if result.returncode != 0:
        raise InstanceError(
            f"refused: cannot confirm that {name} is private "
            f"({_failure(['gh', 'repo', 'view'], result)}). "
            "Check the name and run 'gh auth login'."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InstanceError(f"refused: gh returned unreadable repository data for {name}") from exc
    canonical = str(payload.get("nameWithOwner") or "")
    if not _REPOSITORY_PATTERN.fullmatch(canonical):
        raise InstanceError(f"refused: gh did not report a repository name for {name}")
    if canonical.casefold() in blocked:
        raise InstanceError(f"refused: {canonical} {code_refusal}")
    visibility = str(payload.get("visibility") or "UNKNOWN").upper()
    if visibility != "PRIVATE":
        raise InstanceError(
            f"refused: {canonical} is {visibility.lower()}, not private. Private instance "
            "data (encrypted secrets, tokens, database snapshots) must never go to a public "
            f"repository. Create a private one: gh repo create {canonical.split('/')[0]}/"
            "runway-instance --private"
        )
    return canonical


def _origin_repository(ctx: InstanceContext, instance_dir: Path) -> str | None:
    # The raw configured URL, before any url.<base>.insteadOf rewriting.
    result = ctx.git(instance_dir, "config", "--get", "remote.origin.url", check=False)
    return parse_github_remote(result.stdout) if result.returncode == 0 else None


def _config_entries(result: subprocess.CompletedProcess[str]) -> list[tuple[str, str]]:
    """``(key, value)`` pairs from ``git config -z`` output (``key\nvalue\0`` records)."""
    entries = []
    for record in result.stdout.split("\0"):
        if not record.strip():
            continue
        key, _newline, value = record.partition("\n")
        entries.append((key.strip(), value.strip()))
    return entries


def verify_push_destination(ctx: InstanceContext, instance_dir: Path, repository: str) -> None:
    """Refuse unless every way git could send this clone's data leads to ``repository``."""
    expected = repository.casefold()
    urls = ctx.git(instance_dir, "remote", "get-url", "--push", "--all", "origin", check=False)
    configured = [line.strip() for line in urls.stdout.splitlines() if line.strip()]
    if urls.returncode != 0 or not configured:
        raise InstanceError(f"refused: {instance_dir} has no origin remote for {repository}")
    for url in configured:
        name = parse_github_remote(url)
        if name is None or name.casefold() != expected:
            target = (
                "the public code repository"
                if is_code_repository_remote(url)
                else (name or "an address that is not a plain github.com repository URL")
            )
            raise InstanceError(
                f"refused: the clone's origin pushes to {target}, not {repository}. "
                f"Run: git -C {instance_dir} remote set-url origin {github_url(repository)} "
                "(and remove any remote.origin.pushurl)"
            )
    # A remote *named* like a URL (remote."https://github.com/<repo>.git") takes
    # over an explicit-URL push: git resolves the argument as a remote name first.
    remotes = ctx.git(instance_dir, "config", "-z", "--get-regexp", r"^remote\.", check=False)
    remote_names = {
        key[len("remote.") :].rsplit(".", 1)[0]
        for key, _value in _config_entries(remotes)
        if "." in key[len("remote.") :]
    }
    for remote_name in sorted(remote_names):
        if remote_name != "origin" and (":" in remote_name or "@" in remote_name):
            raise InstanceError(
                f"refused: the clone has a remote named {remote_name!r}; a remote named "
                "like a URL could redirect an explicit-URL push. Keep only 'origin' "
                f"(git -C {instance_dir} remote remove {remote_name})"
            )
    rewrites = ctx.git(
        instance_dir,
        "config",
        "-z",
        "--get-regexp",
        r"^url\..*\.(insteadof|pushinsteadof)$",
        check=False,
    )
    target_url = github_url(repository).casefold()
    for _key, prefix in _config_entries(rewrites):
        if prefix and target_url.startswith(prefix.casefold()):
            raise InstanceError(
                f"refused: a git url.<base>.insteadOf or pushInsteadOf rule rewrites "
                f"{github_url(repository)} to another address; remove it "
                "(git config --show-origin --get-regexp '^url\\.')"
            )


def _require_instance_dir(settings: Settings) -> Path:
    instance_dir = settings.resolved_instance_dir
    if instance_dir is None:
        raise InstanceError(
            "RUNWAY_INSTANCE_DIR is not set. Point it at the private instance clone "
            "(outside this checkout), or run "
            "'runway instance init --repo <owner/name> --dir <path>'"
        )
    return instance_dir


def _configured_repository(ctx: InstanceContext, instance_dir: Path) -> str:
    raw_origin = ctx.git(instance_dir, "config", "--get", "remote.origin.url", check=False)
    origin = _origin_repository(ctx, instance_dir)
    if raw_origin.returncode == 0 and raw_origin.stdout.strip() and origin is None:
        raise InstanceError(
            f"refused: the origin of {instance_dir} is not a plain github.com repository URL"
        )
    configured = ctx.settings.instance_repository
    if configured and origin and configured.casefold() != origin.casefold():
        raise InstanceError(
            f"instance_repository is {configured} but {instance_dir} is a clone of {origin}"
        )
    repository = configured or origin
    if not repository:
        raise InstanceError(
            f"cannot tell which repository {instance_dir} belongs to; "
            "set instance_repository in instance.toml"
        )
    return validate_repository(repository)


def _prepare(ctx: InstanceContext) -> tuple[Path, str]:
    """The instance clone and its confirmed-private repository, for commands that sync."""
    instance_dir = _require_instance_dir(ctx.settings)
    if not (instance_dir / ".git").exists():
        raise InstanceError(
            f"{instance_dir} is not a clone of the private instance repository; "
            "run 'runway instance init --repo <owner/name>' first"
        )
    repository = ensure_private_repository(ctx, _configured_repository(ctx, instance_dir))
    verify_push_destination(ctx, instance_dir, repository)
    return instance_dir, repository


# --- git ---------------------------------------------------------------------------------


def _current_branch(ctx: InstanceContext, instance_dir: Path) -> str:
    result = ctx.git(instance_dir, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise InstanceError(f"{instance_dir} is not on a branch; check it out with git first")
    return branch


def _has_commits(ctx: InstanceContext, instance_dir: Path) -> bool:
    return (
        ctx.git(instance_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode
        == 0
    )


def _remote_ref() -> str:
    """A per-call fetch ref, so concurrent fetches never share one destination ref."""
    return f"{REMOTE_REF}-{uuid.uuid4().hex}"


def _fetch(ctx: InstanceContext, instance_dir: Path, repository: str, remote_ref: str) -> bool:
    """Fetch the branch into ``remote_ref``; False when the remote has no such branch yet."""
    branch = _current_branch(ctx, instance_dir)
    result = ctx.remote_git(
        instance_dir,
        "fetch",
        "--quiet",
        "--no-tags",
        github_url(repository),
        f"+refs/heads/{branch}:{remote_ref}",
        check=False,
    )
    if result.returncode == 0:
        return True
    if "couldn't find remote ref" in (result.stderr or "").lower():
        return False
    raise InstanceError(f"could not fetch {repository} ({_failure(['git', 'fetch'], result)})")


def _git_sync(ctx: InstanceContext, instance_dir: Path, repository: str) -> None:
    remote_ref = _remote_ref()
    try:
        if not _fetch(ctx, instance_dir, repository, remote_ref):
            if _has_commits(ctx, instance_dir):
                raise InstanceError(
                    f"{repository} has no branch {_current_branch(ctx, instance_dir)}"
                )
            return
        result = ctx.git(instance_dir, "merge", "--ff-only", "--quiet", remote_ref, check=False)
        if result.returncode != 0:
            raise InstanceError(
                "could not fast-forward the instance clone from its remote "
                f"({_failure(['git', 'merge'], result)}); resolve it with git in {instance_dir}"
            )
    finally:
        ctx.git(instance_dir, "update-ref", "-d", remote_ref, check=False)


def read_remote_lease(settings: Settings, runner: CommandRunner | None = None) -> Lease | None:
    """``lease.json`` as it is in the private repository right now.

    ``None`` means the remote copy is confirmed absent: the remote branch or
    the file genuinely does not exist. A failed fetch raises instead, so a
    missing answer is never mistaken for "nobody holds the lease".
    """
    ctx = InstanceContext(settings=settings, runner=runner or default_runner)
    instance_dir = _require_instance_dir(settings)
    repository = _configured_repository(ctx, instance_dir)
    with _instance_operation(instance_dir):
        remote_ref = _remote_ref()
        try:
            if not _fetch(ctx, instance_dir, repository, remote_ref):
                return None
            exists = ctx.git(
                instance_dir, "cat-file", "-e", f"{remote_ref}:{LEASE_FILE_NAME}", check=False
            )
            if exists.returncode != 0:
                return None
            shown = ctx.git(instance_dir, "show", f"{remote_ref}:{LEASE_FILE_NAME}")
            return Lease.from_text(shown.stdout)
        finally:
            ctx.git(instance_dir, "update-ref", "-d", remote_ref, check=False)


def _commit(ctx: InstanceContext, instance_dir: Path, message: str, paths: Sequence[str]) -> bool:
    present = [path for path in paths if (instance_dir / path).exists()]
    if not present:
        return False
    ctx.git(instance_dir, "add", "--", *present)
    changed = ctx.git(instance_dir, "diff", "--cached", "--quiet", "--", *present, check=False)
    if changed.returncode == 0:
        return False
    # Commit only these paths; anything else the owner staged stays staged.
    ctx.git(instance_dir, "commit", "--quiet", "-m", message, "--", *present)
    return True


def _push_head(
    ctx: InstanceContext, instance_dir: Path, repository: str
) -> subprocess.CompletedProcess[str]:
    verify_push_destination(ctx, instance_dir, repository)
    branch = _current_branch(ctx, instance_dir)
    # The exact remote/refspec arguments of the real push.
    push_args = ("--no-verify", github_url(repository), f"HEAD:refs/heads/{branch}")
    # Resolve the effective destination first: every `To <url>` line of a
    # --porcelain dry-run is one push URL after all of git's own rewriting
    # (remote-name lookup, insteadOf, pushInsteadOf, every config scope).
    dry_run = ctx.remote_git(
        instance_dir, "push", "--dry-run", "--porcelain", *push_args, check=False
    )
    destinations = [
        line[len("To ") :].strip() for line in dry_run.stdout.splitlines() if line.startswith("To ")
    ]
    expected = repository.casefold()
    for url in destinations:
        name = parse_github_remote(url)
        if name is None or name.casefold() != expected:
            target = name or "an address that is not a plain github.com repository URL"
            raise InstanceError(
                f"refused: git would push to {target}, not {repository}; a remote named "
                "like a URL or a url.<base>.insteadOf rule is redirecting the push. "
                "Nothing was pushed."
            )
    if not destinations:
        raise InstanceError(
            f"refused: could not verify where a push from {instance_dir} would land; "
            f"the dry-run produced no destination ({_failure(['git', 'push'], dry_run)}). "
            "Nothing was pushed."
        )
    if dry_run.returncode != 0:
        # The destination is verified; let the caller treat this as the failed push
        # (for example a non-fast-forward when another machine pushed first).
        return dry_run
    return ctx.remote_git(instance_dir, "push", "--quiet", *push_args, check=False)


def _record_pushed_lease(instance_dir: Path) -> None:
    try:
        lease = read_lease(instance_dir)
    except LeaseError:
        return
    record_remote_check(instance_dir, lease.holder if lease else None, datetime.now(UTC))


def _commit_and_push(
    ctx: InstanceContext,
    instance_dir: Path,
    repository: str,
    message: str,
    paths: Sequence[str],
) -> bool:
    """Commit ``paths`` and push; a rejected push restores every path as it was.

    The fast-forward-only push is the compare-and-swap that arbitrates between
    machines: two machines cannot both build on the same remote commit.
    """
    previous = {
        path: (instance_dir / path).read_bytes() if (instance_dir / path).is_file() else None
        for path in paths
    }
    tracked_before = {
        path
        for path in paths
        if ctx.git(instance_dir, "cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0
    }
    if not _commit(ctx, instance_dir, message, paths):
        return False
    # Re-confirm the repository is still private immediately before pushing to it:
    # _prepare ran a snapshot build ago and the owner may have flipped visibility.
    repository = ensure_private_repository(ctx, repository)
    pushed = _push_head(ctx, instance_dir, repository)
    if pushed.returncode == 0:
        _record_pushed_lease(instance_dir)
        return True
    if _has_parent(ctx, instance_dir):
        ctx.git(instance_dir, "reset", "--quiet", "--soft", "HEAD~1")
    else:
        ctx.git(instance_dir, "update-ref", "-d", "HEAD")
    for path, content in previous.items():
        if path in tracked_before:
            ctx.git(instance_dir, "reset", "--quiet", "HEAD", "--", path)
        else:
            ctx.git(instance_dir, "rm", "--quiet", "--cached", "--ignore-unmatch", "--", path)
        target = instance_dir / path
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(content)
    raise InstanceError(
        "the private repository changed while this command ran, or the push was refused "
        f"({_failure(['git', 'push'], pushed)}). Nothing was uploaded; run "
        "'runway instance status --refresh' and try again."
    )


def _has_parent(ctx: InstanceContext, instance_dir: Path) -> bool:
    return (
        ctx.git(instance_dir, "rev-parse", "--verify", "--quiet", "HEAD~1", check=False).returncode
        == 0
    )


def _publish_lease(
    ctx: InstanceContext, instance_dir: Path, repository: str, lease: Lease, message: str
) -> None:
    previous = (
        (instance_dir / LEASE_FILE_NAME).read_bytes()
        if (instance_dir / LEASE_FILE_NAME).is_file()
        else None
    )
    write_lease(instance_dir, lease)
    try:
        _commit_and_push(ctx, instance_dir, repository, message, [LEASE_FILE_NAME])
    except InstanceError:
        if previous is not None:
            (instance_dir / LEASE_FILE_NAME).write_bytes(previous)
        raise


# --- manifest, revisions, worker ----------------------------------------------------------


def read_manifest(instance_dir: Path) -> dict[str, Any] | None:
    path = instance_dir / MANIFEST_FILE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstanceError(f"{path} cannot be read: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise InstanceError(f"{path} has an unsupported format")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _file_entry(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _script_directory(settings: Settings) -> ScriptDirectory:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    return ScriptDirectory.from_config(config)


def checkout_revisions(settings: Settings) -> set[str]:
    return {revision.revision for revision in _script_directory(settings).walk_revisions()}


def ensure_snapshot_revision_supported(settings: Settings, revision: str | None) -> None:
    if revision is None or revision in checkout_revisions(settings):
        return
    raise InstanceError(
        f"the snapshot's database is at migration {revision}, which this code checkout does "
        "not have: the snapshot is newer than the code. Update the code first "
        "(git pull in the Runway checkout), then run 'runway instance pull' again."
    )


def _local_database_revision(settings: Settings) -> str | None:
    import sqlite3
    from contextlib import closing

    path = settings.database_path
    if not path.is_file():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def wait_for_idle_worker(ctx: InstanceContext, wait_seconds: float) -> None:
    deadline = ctx.now() + timedelta(seconds=wait_seconds)
    while ctx.worker_busy(ctx.settings):
        if ctx.now() >= deadline:
            markers = ", ".join(str(path) for path in worker_busy_markers(ctx.settings))
            raise InstanceError(
                "a publish or other platform write is still running"
                + (f" ({markers})" if markers else "")
                + "; try again when it has finished. A marker left by a crashed process can be "
                "deleted by hand."
            )
        ctx.sleep(WORKER_POLL_SECONDS)


def _code_revision(ctx: InstanceContext) -> tuple[str, str]:
    root = ctx.settings.project_root
    try:
        commit = ctx.git(root, "rev-parse", "HEAD", check=False)
        branch = ctx.git(root, "branch", "--show-current", check=False)
    except InstanceError:
        return "unknown", "unknown"
    return (
        commit.stdout.strip() if commit.returncode == 0 and commit.stdout.strip() else "unknown",
        branch.stdout.strip() if branch.returncode == 0 and branch.stdout.strip() else "unknown",
    )


# --- age and sops -----------------------------------------------------------------------


def age_key_file() -> Path:
    """Where SOPS looks for this user's age identities."""
    explicit = os.environ.get("SOPS_AGE_KEY_FILE")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "sops" / "age" / "keys.txt"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "sops"
            / "age"
            / "keys.txt"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "sops" / "age" / "keys.txt"
    return Path.home() / ".config" / "sops" / "age" / "keys.txt"


def _ensure_age_recipient(ctx: InstanceContext) -> tuple[str, bool]:
    """This machine's age public key, generating the key pair if none exists.

    The private key stays in the user profile; Python never reads it.
    """
    key_file = age_key_file()
    created = False
    if not key_file.is_file():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        ctx.run(["age-keygen", "-o", str(key_file)])
        created = True
    result = ctx.run(["age-keygen", "-y", str(key_file)])
    lines = result.stdout.strip().splitlines()
    recipient = lines[0].strip() if lines else ""
    if not _AGE_RECIPIENT_PATTERN.fullmatch(recipient):
        raise InstanceError(f"age-keygen did not return an age public key for {key_file}")
    return recipient, created


def _sops_config(recipients: Sequence[str]) -> str:
    return (
        "# One age public key per trusted machine. After changing this list run\n"
        "# 'sops updatekeys secrets.enc.env' on a machine that can already decrypt it.\n"
        "creation_rules:\n"
        "  - path_regex: secrets\\.enc\\.env$\n"
        f"    age: {','.join(recipients)}\n"
    )


def _sops_recipients(text: str) -> list[str]:
    return list(dict.fromkeys(_AGE_RECIPIENT_PATTERN.findall(text)))


def _add_sops_recipient(path: Path, recipient: str) -> bool:
    """Append this machine to the managed ``age:`` line; False when it needs a human."""
    text = path.read_text(encoding="utf-8")
    lines = _SOPS_AGE_LINE.findall(text)
    if len(lines) != 1:
        return False
    updated = _SOPS_AGE_LINE.sub(
        lambda match: f"{match.group('prefix')}{match.group('value')},{recipient}", text, count=1
    )
    path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def _private_directory(ctx: InstanceContext, directory: Path) -> None:
    """Create ``directory`` readable by the current user only."""
    directory.mkdir(mode=0o700)
    if sys.platform == "win32":
        domain = os.environ.get("USERDOMAIN")
        user = os.environ.get("USERNAME") or getpass.getuser()
        principal = f"{domain}\\{user}" if domain else user
        # Drop inherited entries (Users, Authenticated Users, ...) and grant only this user.
        ctx.run(["icacls", str(directory), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F"])
    else:
        directory.chmod(0o700)


def _encrypt_secrets(
    ctx: InstanceContext,
    instance_dir: Path,
    destination: Path,
    recipients: Sequence[str],
    values: dict[str, str],
) -> None:
    """Create ``secrets.enc.env`` with SOPS.

    SOPS reads its plaintext from a file. That file lives for one ``sops`` call
    in the instance's ignored ``tmp`` directory (never the shared temp
    directory), restricted to the current user, and is deleted in ``finally``.
    """
    plaintext = "".join(f"{key}={value}\n" for key, value in values.items())
    tmp_root = instance_dir / INSTANCE_TMP_DIR
    tmp_root.mkdir(exist_ok=True)
    workdir = tmp_root / f"secrets-{os.getpid()}-{os.urandom(4).hex()}"
    source = workdir / "secrets.env"
    try:
        _private_directory(ctx, workdir)
        fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(plaintext)
        ctx.run(
            [
                "sops",
                "--encrypt",
                "--age",
                ",".join(recipients),
                "--input-type",
                "dotenv",
                "--output-type",
                "dotenv",
                "--output",
                str(destination),
                str(source),
            ]
        )
    finally:
        source.unlink(missing_ok=True)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
    written = destination.read_bytes() if destination.is_file() else b""
    if not written or any(
        # Only long values: short ones (an app id) can occur in SOPS's plain key names.
        len(value) >= 16 and value.encode("utf-8") in written
        for value in values.values()
    ):
        destination.unlink(missing_ok=True)
        raise InstanceError("sops did not produce an encrypted secrets.enc.env; nothing was kept")


def _legacy_token_key(settings: Settings) -> bytes | None:
    path = settings.resolved_data_dir / "secrets" / "token.key"
    if not path.is_file():
        return None
    key = path.read_bytes().strip()
    try:
        Fernet(key)
    except ValueError as exc:
        raise InstanceError(f"{path} is not a valid token key") from exc
    return key


# --- commands ----------------------------------------------------------------------------


def _write_if_missing(path: Path, text: str, created: list[str], name: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    created.append(name)


def init(ctx: InstanceContext, repository: str, *, directory: Path | None = None) -> dict[str, Any]:
    """Clone (or adopt) the private repository and create the instance layout."""
    settings = ctx.settings
    repo = ensure_private_repository(ctx, repository)
    if directory is not None:
        instance_dir = resolve_instance_dir(directory, settings.project_root)
    else:
        instance_dir = _require_instance_dir(settings)

    if not instance_dir.exists() or (instance_dir.is_dir() and not any(instance_dir.iterdir())):
        # The lock lives under <instance dir>/tmp, so it is taken only after the
        # clone exists: git clone needs an empty directory.
        instance_dir.parent.mkdir(parents=True, exist_ok=True)
        ctx.run(["gh", "repo", "clone", repo, str(instance_dir)])
    elif not (instance_dir / ".git").exists():
        raise InstanceError(
            f"{instance_dir} exists and is not a git clone; choose an empty or new directory"
        )
    with _instance_operation(instance_dir):
        return _init(ctx, repo, instance_dir)


def _init(ctx: InstanceContext, repo: str, instance_dir: Path) -> dict[str, Any]:
    settings = ctx.settings
    origin = _origin_repository(ctx, instance_dir)
    if origin is None or origin.casefold() != repo.casefold():
        raise InstanceError(
            f"{instance_dir} is a clone of {origin or 'an unrecognized remote'}, not {repo}"
        )
    verify_push_destination(ctx, instance_dir, repo)
    _git_sync(ctx, instance_dir, repo)

    new_instance = (
        not (instance_dir / LEASE_FILE_NAME).exists()
        and not (instance_dir / MANIFEST_FILE_NAME).exists()
    )
    recipient, key_created = _ensure_age_recipient(ctx)
    created: list[str] = []
    next_steps: list[str] = []

    _write_if_missing(instance_dir / "README.md", _README, created, "README.md")
    _write_if_missing(instance_dir / ".gitignore", _GITIGNORE, created, ".gitignore")
    _write_if_missing(instance_dir / ".gitattributes", _GITATTRIBUTES, created, ".gitattributes")
    _write_if_missing(
        instance_dir / INSTANCE_TOML_NAME,
        "# Shared, non-secret Runway settings: setting names without RUNWAY_.\n"
        "# Secrets go in secrets.enc.env (sops edit secrets.enc.env), never here.\n"
        "# Machine-local settings (machine_id, paths, executables) belong in each\n"
        "# machine's environment and are refused here.\n"
        f'instance_repository = "{repo}"\n'
        '# channel_name = "Qlob"\n'
        '# timezone = "America/Toronto"\n',
        created,
        INSTANCE_TOML_NAME,
    )

    sops_config = instance_dir / SOPS_CONFIG_NAME
    if not sops_config.exists():
        _write_if_missing(sops_config, _sops_config([recipient]), created, SOPS_CONFIG_NAME)
    elif recipient not in sops_config.read_text(encoding="utf-8"):
        if _add_sops_recipient(sops_config, recipient):
            created.append(f"{SOPS_CONFIG_NAME} (added this machine)")
        else:
            next_steps.append(f"Add {recipient} to the age recipients in {SOPS_CONFIG_NAME}.")
        next_steps.append(
            "On a machine that can already decrypt the secrets, run "
            f"'sops updatekeys {SECRETS_FILE_NAME}' and then 'runway instance push'."
        )
    recipients = _sops_recipients(sops_config.read_text(encoding="utf-8"))

    tokens_file = instance_dir / TOKENS_FILE
    (instance_dir / TOKENS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    secrets_file = instance_dir / SECRETS_FILE_NAME
    imported_tokens = False
    if not secrets_file.exists():
        legacy_key = _legacy_token_key(settings)
        legacy_store = settings.resolved_data_dir / "secrets" / "platform-tokens.json"
        if legacy_key is not None and legacy_store.is_file() and not tokens_file.exists():
            shutil.copy2(legacy_store, tokens_file)
            imported_tokens = True
        values = {"RUNWAY_TOKEN_KEY": (legacy_key or Fernet.generate_key()).decode("ascii")}
        for field_name, env_name in MIGRATED_SECRET_SETTINGS:
            value = getattr(settings, field_name)
            if value:
                values[env_name] = str(value)
        _encrypt_secrets(ctx, instance_dir, secrets_file, recipients or [recipient], values)
        created.append(SECRETS_FILE_NAME)
        if legacy_key is not None:
            next_steps.append(
                "The token key was copied into secrets.enc.env. Once Runway runs against the "
                f"instance, delete {settings.resolved_data_dir / 'secrets' / 'token.key'}."
            )
        if len(values) > 1:
            next_steps.append(
                "Platform client ids and secrets were copied into secrets.enc.env; remove them "
                "from the checkout's .env."
            )

    if new_instance:
        now = ctx.now()
        write_lease(instance_dir, Lease(holder=ctx.machine, holder_since=now, heartbeat_at=now))
        created.append(LEASE_FILE_NAME)

    committed = _commit_and_push(
        ctx, instance_dir, repo, "Initialize the Runway instance layout", TRACKED_FILES
    )

    if settings.resolved_instance_dir != instance_dir:
        next_steps.insert(0, f"Set RUNWAY_INSTANCE_DIR={instance_dir} for Runway on this machine.")
    if not new_instance and not instance_is_active_at(instance_dir, ctx.machine):
        next_steps.append(
            "This machine is in standby. Run 'runway instance handover' on the active machine, "
            "then 'runway instance pull' here."
        )
    lease = read_lease(instance_dir)
    return {
        "repository": repo,
        "instance_dir": str(instance_dir),
        "machine": ctx.machine,
        "age_recipient": recipient,
        "age_key_created": key_created,
        "created": created,
        "imported_token_store": imported_tokens,
        "lease": describe_lease(lease),
        "committed": committed,
        "next_steps": next_steps,
    }


def instance_is_active_at(instance_dir: Path, machine: str) -> bool:
    try:
        lease = read_lease(instance_dir)
    except LeaseError:
        return False
    return lease is not None and lease.holder == machine


def status(ctx: InstanceContext, *, refresh: bool = False) -> dict[str, Any]:
    """Lease holder, last snapshot and drift between local state and the manifest."""
    settings = ctx.settings
    instance_dir = _require_instance_dir(settings)
    report: dict[str, Any] = {
        "instance_dir": str(instance_dir),
        "machine": ctx.machine,
        "repository": settings.instance_repository,
    }
    if not (instance_dir / ".git").exists():
        report["state"] = "not initialized; run 'runway instance init --repo <owner/name>'"
        return report
    if refresh:
        _instance_dir, repository = _prepare(ctx)
        with _instance_operation(instance_dir):
            _git_sync(ctx, instance_dir, repository)
    lease = read_lease(instance_dir)
    now = ctx.now()
    report["lease"] = {
        "description": describe_lease(lease),
        "holder": lease.holder if lease else None,
        "stale": lease.is_stale(now) if lease else False,
    }
    report["active"] = instance_is_active(settings)
    report["mode"] = "active" if report["active"] else "standby"
    report["worker_busy"] = [str(path) for path in worker_busy_markers(settings)]

    manifest = read_manifest(instance_dir)
    drift: list[str] = []
    warnings: list[str] = []
    if manifest is None:
        report["last_snapshot"] = None
        drift.append("no snapshot has been pushed yet")
    else:
        report["last_snapshot"] = {
            "created_at": manifest.get("created_at"),
            "machine": manifest.get("machine"),
            "tag": manifest.get("release", {}).get("tag"),
            "code_commit": manifest.get("code", {}).get("commit"),
            "alembic_revision": manifest.get("alembic_revision"),
        }
        files = manifest.get("files", {})
        if _file_entry(instance_dir / TOKENS_FILE) != files.get(TOKENS_FILE):
            drift.append("the token store changed since the snapshot")
        local_revision = _local_database_revision(settings)
        if local_revision != manifest.get("alembic_revision"):
            drift.append(
                f"local database migration {local_revision} differs from the snapshot's "
                f"{manifest.get('alembic_revision')}"
            )
        if _local_database_is_newer(settings, manifest):
            drift.append("the local database changed after the snapshot")
        revision = manifest.get("alembic_revision")
        if isinstance(revision, str) and revision not in checkout_revisions(settings):
            warnings.append("the snapshot is newer than this code checkout; update the code")
    try:
        porcelain = ctx.git(instance_dir, "status", "--porcelain", check=False)
        uncommitted = [line[3:] for line in porcelain.stdout.splitlines() if line.strip()]
    except InstanceError:
        uncommitted = []
    if uncommitted:
        drift.append("uncommitted changes in the instance clone: " + ", ".join(uncommitted))
    if not (instance_dir / SECRETS_FILE_NAME).is_file():
        warnings.append(f"{SECRETS_FILE_NAME} is missing; the token store has no key")
    if (settings.resolved_data_dir / "secrets" / "token.key").is_file():
        warnings.append("a plain data/secrets/token.key still exists; delete it once migrated")
    report["drift"] = drift
    report["warnings"] = warnings
    return report


def _local_database_is_newer(settings: Settings, manifest: dict[str, Any]) -> bool:
    database = settings.database_path
    created_at = manifest.get("created_at")
    if not database.is_file() or not isinstance(created_at, str):
        return False
    snapshot_at = datetime.fromisoformat(created_at)
    # Runway runs SQLite in WAL mode: recent commits can sit in runway.db-wal
    # while the main file's mtime does not move, so a non-empty WAL written
    # after the snapshot is newer local work even when the .db looks unchanged.
    wal = database.parent / f"{database.name}-wal"
    if (
        wal.is_file()
        and wal.stat().st_size > 0
        and datetime.fromtimestamp(wal.stat().st_mtime, UTC) > snapshot_at
    ):
        return True
    modified = datetime.fromtimestamp(database.stat().st_mtime, UTC)
    if modified <= snapshot_at:
        return False
    return sha256_file(database) != str(manifest.get("database", {}).get("sha256"))


def _verify_release_assets(
    ctx: InstanceContext, repository: str, tag: str, assets: dict[str, Any]
) -> None:
    release_info = ctx.run(["gh", "api", f"repos/{repository}/releases/tags/{tag}"])
    try:
        uploaded = {
            str(asset["name"]): asset for asset in json.loads(release_info.stdout).get("assets", [])
        }
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
        raise InstanceError("gh returned unreadable release data") from exc
    for name, entry in assets.items():
        asset = uploaded.get(name)
        if entry is None or asset is None or int(asset.get("size", -1)) != entry["bytes"]:
            raise InstanceError(
                f"uploaded snapshot asset {name} ({tag}) is missing or has the wrong size"
            )
        digest = asset.get("digest")
        if digest and digest != f"sha256:{entry['sha256']}":
            raise InstanceError(f"uploaded snapshot asset {name} ({tag}) has the wrong digest")


def _upload_snapshot(
    ctx: InstanceContext, repository: str, tag: str, workdir: Path, assets: dict[str, Any]
) -> None:
    """Upload to the immutable dated release, then refresh ``snapshot-latest``."""
    files = [str(workdir / name) for name in ASSET_NAMES]
    ctx.run(
        [
            "gh",
            "release",
            "create",
            tag,
            *files,
            "--repo",
            repository,
            "--title",
            f"Runway instance {tag}",
            "--notes",
            _RELEASE_NOTES,
            "--prerelease",
        ]
    )
    _verify_release_assets(ctx, repository, tag, assets)
    exists = ctx.run(
        ["gh", "release", "view", SNAPSHOT_TAG, "--repo", repository, "--json", "tagName"],
        check=False,
    )
    if exists.returncode != 0:
        ctx.run(
            [
                "gh",
                "release",
                "create",
                SNAPSHOT_TAG,
                "--repo",
                repository,
                "--title",
                "Runway instance snapshot (latest)",
                "--notes",
                _RELEASE_NOTES,
                "--prerelease",
            ]
        )
    ctx.run(["gh", "release", "upload", SNAPSHOT_TAG, *files, "--clobber", "--repo", repository])
    _verify_release_assets(ctx, repository, SNAPSHOT_TAG, assets)


def _prune_snapshots(ctx: InstanceContext, repository: str, keep: int, current: str) -> list[str]:
    listed = ctx.run(
        ["gh", "release", "list", "--repo", repository, "--limit", "1000", "--json", "tagName"],
        check=False,
    )
    if listed.returncode != 0:
        logger.warning(
            "could not list snapshot releases to prune: %s", scrub_tool_output(listed.stderr)
        )
        return []
    try:
        tags = [str(entry["tagName"]) for entry in json.loads(listed.stdout)]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    dated = sorted((tag for tag in tags if _DATED_TAG_PATTERN.fullmatch(tag)), reverse=True)
    deleted: list[str] = []
    for tag in dated[keep:]:
        if tag == current:
            continue
        result = ctx.run(
            ["gh", "release", "delete", tag, "--repo", repository, "--yes", "--cleanup-tag"],
            check=False,
        )
        if result.returncode == 0:
            deleted.append(tag)
    return deleted


def push(
    ctx: InstanceContext, *, wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS
) -> dict[str, Any]:
    """Snapshot the database and media, arbitrate through git, then upload."""
    with _instance_operation(_require_instance_dir(ctx.settings)):
        return _push(ctx, wait_seconds=wait_seconds)


def _push(ctx: InstanceContext, *, wait_seconds: float) -> dict[str, Any]:
    settings = ctx.settings
    instance_dir, repository = _prepare(ctx)
    _git_sync(ctx, instance_dir, repository)
    me = ctx.machine
    lease = read_lease(instance_dir)  # the remote copy: the clone was just fast-forwarded
    if lease is None or lease.holder != me:
        raise LeaseError(
            f"only the machine holding the lease may push ({describe_lease(lease)}). "
            "To make this machine active, run 'runway instance pull'."
        )
    # The transfer marker makes this machine standby for new work before the
    # idle wait: a job that slips in after the check would land its writes
    # after the snapshot, and the next machine would repeat them.
    with instance_transfer(settings, "push"):
        wait_for_idle_worker(ctx, wait_seconds)
        database = settings.database_path
        if not database.is_file():
            raise InstanceError(f"there is no database at {database} to snapshot")
        commit, branch = _code_revision(ctx)
        now = ctx.now()
        tag = f"{DATED_TAG_PREFIX}{now.astimezone(UTC).strftime('%Y%m%d-%H%M%S')}"
        with tempfile.TemporaryDirectory(prefix="runway-instance-push-") as temporary:
            workdir = Path(temporary)
            snapshot = create_release(
                source_database=database,
                data_root=settings.resolved_data_dir,
                output_directory=workdir,
                commit_sha=commit,
                branch=branch,
            )
            assets = {name: _file_entry(workdir / name) for name in ASSET_NAMES}
            manifest: dict[str, Any] = {
                "format_version": MANIFEST_FORMAT_VERSION,
                "created_at": now.isoformat(),
                "machine": me,
                "repository": repository,
                "code": {
                    "repository": CODE_REPOSITORY,
                    "commit": snapshot["commit_sha"],
                    "branch": branch,
                },
                "alembic_revision": snapshot["migration"],
                "database": {
                    "bytes": snapshot["database_bytes"],
                    "sha256": snapshot["database_sha256"],
                    "table_count": snapshot["table_count"],
                },
                "media": {
                    "entry_count": snapshot["database_media"]["entry_count"],
                    "content_bytes": snapshot["database_media"]["content_bytes"],
                },
                "release": {"tag": tag, "latest_tag": SNAPSHOT_TAG, "assets": assets},
                "files": {
                    name: entry
                    for name in (TOKENS_FILE, SECRETS_FILE_NAME, INSTANCE_TOML_NAME)
                    if (entry := _file_entry(instance_dir / name)) is not None
                },
            }
            originals = {
                name: (instance_dir / name).read_bytes()
                if (instance_dir / name).is_file()
                else None
                for name in (MANIFEST_FILE_NAME, LEASE_FILE_NAME)
            }
            _write_json(instance_dir / MANIFEST_FILE_NAME, manifest)
            write_lease(instance_dir, acquire(lease, me, now, take_stale=False))
            try:
                # Arbitrate first: only after the fast-forward push wins is anything uploaded.
                _commit_and_push(
                    ctx, instance_dir, repository, f"Snapshot {tag} from {me}", TRACKED_FILES
                )
            except InstanceError:
                for name, content in originals.items():
                    if content is None:
                        (instance_dir / name).unlink(missing_ok=True)
                    else:
                        (instance_dir / name).write_bytes(content)
                raise
            # The lease/manifest commit won the arbitration; re-confirm privacy once
            # more before the snapshot assets leave this machine.
            ensure_private_repository(ctx, repository)
            _upload_snapshot(ctx, repository, tag, workdir, assets)
    pruned = _prune_snapshots(ctx, repository, settings.instance_snapshot_keep, tag)
    return {
        "status": "pushed",
        "repository": repository,
        "machine": me,
        "tag": tag,
        "alembic_revision": manifest["alembic_revision"],
        "code_commit": manifest["code"]["commit"],
        "assets": {name: entry["bytes"] for name, entry in assets.items() if entry},
        "pruned": pruned,
    }


def handover(
    ctx: InstanceContext, *, wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS
) -> dict[str, Any]:
    """Push, then release the lease so another machine can pull."""
    settings = ctx.settings
    # The marker stays set through the release publish: this machine must
    # not run new work between the snapshot and the lease release.
    with (
        _instance_operation(_require_instance_dir(settings)),
        instance_transfer(settings, "handover"),
    ):
        result = _push(ctx, wait_seconds=wait_seconds)
        instance_dir, repository = _prepare(ctx)
        released = release(read_lease(instance_dir), ctx.machine, ctx.now())
        _publish_lease(
            ctx,
            instance_dir,
            repository,
            released,
            f"Hand over the lease from {ctx.machine}",
        )
        return {**result, "status": "handed over", "mode": "standby"}


def heartbeat(
    ctx: InstanceContext,
    *,
    renew_after: timedelta = HEARTBEAT_RENEW_AFTER,
    lock_timeout: float | None = None,
) -> dict[str, Any]:
    """Renew this machine's lease heartbeat in the private repository when it is due."""
    with _instance_operation(_require_instance_dir(ctx.settings), timeout=lock_timeout):
        instance_dir, repository = _prepare(ctx)
        _git_sync(ctx, instance_dir, repository)
        me = ctx.machine
        lease = read_lease(instance_dir)
        record_remote_check(instance_dir, lease.holder if lease else None, datetime.now(UTC))
        if lease is None or lease.holder != me:
            return {"status": "standby", "lease": describe_lease(lease)}
        now = ctx.now()
        if lease.heartbeat_at is not None and now - lease.heartbeat_at < renew_after:
            return {"status": "current", "lease": describe_lease(lease)}
        renewed = replace(lease, heartbeat_at=now)
        _publish_lease(ctx, instance_dir, repository, renewed, f"Heartbeat from {me}")
        return {"status": "renewed", "lease": describe_lease(renewed)}


class LeaseHeartbeat:
    """Background renewal of the lease heartbeat while Runway runs (spec 07 §4)."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval_seconds: float = HEARTBEAT_THREAD_INTERVAL_SECONDS,
        context_factory: Callable[[Settings], InstanceContext] | None = None,
    ) -> None:
        self.settings = settings
        self.interval_seconds = interval_seconds
        # default_runner is looked up at each beat, so tests can replace it in one place.
        self._context_factory = context_factory or (
            lambda value: InstanceContext(settings=value, runner=default_runner)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] | None = None

    def beat(self) -> None:
        try:
            self.last_result = heartbeat(self._context_factory(self.settings), lock_timeout=0.0)
        except InstanceBusyError:
            # A CLI command (or another beat) holds the clone: this beat is
            # skipped, not failed; the next one runs on schedule.
            self.last_result = {
                "status": "skipped",
                "reason": "another instance operation is running",
            }
        except (
            InstanceError,
            InstanceConfigError,
            LeaseError,
            DatabaseReleaseError,
            OSError,
        ) as exc:
            self.last_result = {"status": "failed"}
            logger.warning("instance lease heartbeat failed: %s", scrub_tool_output(str(exc)))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.beat()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="runway-lease-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _download_snapshot(
    ctx: InstanceContext, repository: str, manifest: dict[str, Any], destination: Path
) -> None:
    release_info = manifest.get("release", {})
    tag = str(release_info.get("tag") or SNAPSHOT_TAG)
    ctx.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            *(part for name in ASSET_NAMES for part in ("--pattern", name)),
            "--dir",
            str(destination),
            "--clobber",
        ]
    )
    expected = release_info.get("assets", {})
    for name in ASSET_NAMES:
        if _file_entry(destination / name) != expected.get(name):
            raise InstanceError(
                f"downloaded snapshot asset {name} ({tag}) does not match manifest.json; "
                "the release is incomplete or out of step. Ask the active machine to push again."
            )


def _database_can_be_replaced(database: Path) -> bool:
    """True when no process holds ``database`` open (Windows refuses the rename)."""
    if not database.is_file():
        return True
    probe = database.with_name(f"{database.name}.pull-check")
    try:
        os.replace(database, probe)
        os.replace(probe, database)
    except OSError:
        # Best effort: never leave the database at the probe path.
        if probe.is_file() and not database.exists():
            with suppress(OSError):
                os.replace(probe, database)
        return False
    return True


def _refuse_while_runway_is_running(settings: Settings) -> None:
    """Refuse a pull while this machine's Runway is running.

    The API holds ``runtime/worker.lock`` for its whole lifetime, and on
    Windows an open SQLite file cannot even be renamed. Both probes run before
    the lease is taken: otherwise a failed install would leave this machine
    holding the lease on the stale standby database.
    """
    try:
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            pass
    except FileLockTimeout as exc:
        raise InstanceError(
            "stop Runway (API and worker) before pulling: it is running on this "
            f"machine and holds {worker_lock_path(settings)}"
        ) from exc
    database = settings.database_path
    if not _database_can_be_replaced(database):
        raise InstanceError(
            "stop Runway (API and worker) before pulling: the local database "
            f"{database} is open, so the snapshot cannot be installed"
        )


def _install_backup_dir(data_dir: Path, stamp: str) -> Path:
    return data_dir / "backups" / f"pre-instance-pull-{stamp}"


def _restore_install_backup(backup_dir: Path, data_dir: Path) -> None:
    """Move every file a partial install backed up back where it came from."""
    if not backup_dir.is_dir():
        return
    for source in sorted(backup_dir.rglob("*")):
        if not source.is_file():
            continue
        destination = data_dir / source.relative_to(backup_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError:
            logger.warning("could not restore %s after the failed install", destination)
    shutil.rmtree(backup_dir, ignore_errors=True)


def _release_taken_lease(
    ctx: InstanceContext, instance_dir: Path, repository: str, me: str
) -> bool:
    """Best effort: undo a just-published lease take after a failed install."""
    try:
        released = release(read_lease(instance_dir), me, ctx.now())
        _publish_lease(
            ctx,
            instance_dir,
            repository,
            released,
            f"Release the lease on {me} after a failed install",
        )
    except (InstanceError, InstanceConfigError, LeaseError, OSError) as exc:
        logger.warning("could not release the lease after the failed install: %s", exc)
        return False
    return True


def _install_restored(data_dir: Path, staging: Path, stamp: str) -> Path | None:
    """Move the verified restore into the data directory, backing up what it replaces."""
    backup_dir = _install_backup_dir(data_dir, stamp)
    moved_any = False

    def back_up(path: Path, relative: Path) -> None:
        nonlocal moved_any
        target = backup_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
        moved_any = True

    for suffix in ("", "-wal", "-shm"):
        existing = data_dir / f"runway.db{suffix}"
        if existing.exists():
            back_up(existing, Path(existing.name))
    os.replace(staging / "runway.db", data_dir / "runway.db")
    media_root = staging / "media"
    if media_root.is_dir():
        for source in sorted(media_root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(staging)
            destination = data_dir / relative
            if destination.exists():
                if sha256_file(destination) == sha256_file(source):
                    continue
                back_up(destination, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
    return backup_dir if moved_any else None


def pull(
    ctx: InstanceContext,
    *,
    take: bool = False,
    force: bool = False,
    i_know_the_other_machine_is_off: bool = False,
    wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS,
) -> dict[str, Any]:
    """Download and verify the latest snapshot, take the lease, then install it."""
    with _instance_operation(_require_instance_dir(ctx.settings)):
        return _pull(
            ctx,
            take=take,
            force=force,
            i_know_the_other_machine_is_off=i_know_the_other_machine_is_off,
            wait_seconds=wait_seconds,
        )


def _refuse_a_recent_take(
    ctx: InstanceContext, instance_dir: Path, remote_lease: Lease | None, me: str
) -> None:
    """The clock-skew guard for ``--take``, measured on the holder's own clock.

    ``heartbeat_at`` is the holder's self-reported time and ``ctx.now`` is this
    machine's clock; skew in either makes a live lease look stale. The fetched
    lease commit's committer timestamp is in the same clock domain as the
    heartbeat, so ``committed - heartbeat`` is the heartbeat's true age on the
    holder's own timeline. Younger than three stale intervals means the
    holder's clock admits the lease was fresh when last committed.
    """
    if remote_lease is None or remote_lease.holder is None or remote_lease.holder == me:
        return
    heartbeat = remote_lease.heartbeat_at
    if heartbeat is None:
        return
    result = ctx.git(instance_dir, "log", "-1", "--format=%ct", "HEAD", check=False)
    committed: datetime | None = None
    if result.returncode == 0:
        try:
            committed = datetime.fromtimestamp(int(result.stdout.strip()), UTC)
        except ValueError:
            committed = None
    if committed is not None and committed - heartbeat >= 3 * LEASE_STALE_AFTER:
        return
    holder_heartbeat = heartbeat.astimezone(UTC).isoformat()
    raise LeaseError(
        f"{remote_lease.holder} holds the lease (last heartbeat {holder_heartbeat}), and "
        "the fetched lease commit does not prove that heartbeat old on the holder's own "
        "clock — the staleness you see may be clock skew on either machine. If that "
        "machine is truly off, confirm with 'runway instance pull --take "
        "--i-know-the-other-machine-is-off'."
    )


def _pull(
    ctx: InstanceContext,
    *,
    take: bool,
    force: bool,
    i_know_the_other_machine_is_off: bool,
    wait_seconds: float,
) -> dict[str, Any]:
    settings = ctx.settings
    instance_dir, repository = _prepare(ctx)
    _git_sync(ctx, instance_dir, repository)
    me = ctx.machine
    now = ctx.now()
    # After the fast-forward the clone's lease.json is the remote copy, so a --take
    # is judged by the remote heartbeat, not by whatever this clone last saw.
    remote_lease = read_lease(instance_dir)
    new_lease = acquire(remote_lease, me, now, take_stale=take)
    if take and not i_know_the_other_machine_is_off:
        _refuse_a_recent_take(ctx, instance_dir, remote_lease, me)
    manifest = read_manifest(instance_dir)
    if manifest is None:
        raise InstanceError("no snapshot has been pushed yet; run 'runway instance push' first")
    revision = manifest.get("alembic_revision")
    ensure_snapshot_revision_supported(settings, revision if isinstance(revision, str) else None)
    already_holder = remote_lease is not None and remote_lease.holder == me
    if already_holder and not force and _local_database_is_newer(settings, manifest):
        raise InstanceError(
            "the local database changed after the latest snapshot, so restoring it would lose "
            "work. Run 'runway instance push' to keep it, or 'runway instance pull --force' to "
            "replace it (the local database is backed up under data/backups first)."
        )
    _refuse_while_runway_is_running(settings)
    wait_for_idle_worker(ctx, wait_seconds)

    warnings: list[str] = []
    if _file_entry(instance_dir / TOKENS_FILE) != manifest.get("files", {}).get(TOKENS_FILE):
        warnings.append("the token store is newer than the snapshot; keeping the newer tokens")
    data_dir = settings.resolved_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="runway-instance-pull-") as download,
        tempfile.TemporaryDirectory(dir=data_dir, prefix=".instance-pull-") as staging,
    ):
        download_dir = Path(download)
        _download_snapshot(ctx, repository, manifest, download_dir)
        verified = verify_release(
            manifest_path=download_dir / MANIFEST_NAME,
            database_archive=download_dir / DATABASE_ARCHIVE_NAME,
            media_archive=download_dir / MEDIA_ARCHIVE_NAME,
            output_directory=Path(staging),
        )
        if verified["migration"] != revision:
            raise InstanceError("the snapshot's database revision does not match manifest.json")
        # Only a verified snapshot is worth the lease; the push arbitrates between machines.
        _publish_lease(ctx, instance_dir, repository, new_lease, f"Take the lease on {me}")
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        try:
            backup = _install_restored(data_dir, Path(staging), stamp)
        except OSError as exc:
            # A half-installed snapshot must not leave this machine holding the
            # lease on its old database: put back what the install moved and
            # release the lease it just took.
            _restore_install_backup(_install_backup_dir(data_dir, stamp), data_dir)
            if _release_taken_lease(ctx, instance_dir, repository, me):
                state = "the lease was released, so this machine stays in standby"
            else:
                state = (
                    "the lease could not be released; another machine can still "
                    "take it with --take once it is stale"
                )
            raise InstanceError(
                f"the snapshot could not be installed: {exc}. The previous "
                f"local database was restored and {state}. Stop Runway (API "
                "and worker), then run 'runway instance pull' again."
            ) from exc
    return {
        "status": "pulled",
        "repository": repository,
        "machine": me,
        "mode": "active",
        "tag": manifest.get("release", {}).get("tag"),
        "alembic_revision": revision,
        "snapshot_created_at": manifest.get("created_at"),
        "snapshot_machine": manifest.get("machine"),
        "database": str(settings.database_path),
        "replaced_files_backup": str(backup) if backup else None,
        "warnings": warnings,
        "next_steps": (
            ["The code is newer than the snapshot; Runway applies the migrations when it starts."]
            if revision not in set(_script_directory(settings).get_heads())
            else []
        ),
    }
