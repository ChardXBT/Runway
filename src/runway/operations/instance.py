"""``runway instance``: keep this instance's private state in a private companion repository.

Spec 07. The code repository stays public; settings, encrypted secrets, the
token store, the lease and a manifest live in a separate **private** repository,
and database and media snapshots travel as that repository's release assets.

Every external tool (``gh``, ``git``, ``sops``, ``age-keygen``) runs through an
injectable :class:`CommandRunner` with an argument list and no shell. No secret
is ever placed on a command line or in an error message.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from runway.config import Settings
from runway.config.instance import (
    INSTANCE_TOML_NAME,
    SECRETS_FILE_NAME,
    SOPS_INSTALL_HINT,
    TOKENS_DIR_NAME,
    InstanceConfigError,
    resolve_instance_dir,
)
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
    LEASE_FILE_NAME,
    Lease,
    LeaseError,
    StandbyError,
    WorkerBusyCheck,
    acquire,
    describe_lease,
    ensure_active,
    instance_is_active,
    machine_id,
    read_lease,
    release,
    worker_is_busy,
    write_lease,
)
from runway.platforms.base import safe_error_summary

__all__ = [
    "CODE_REPOSITORY",
    "SNAPSHOT_TAG",
    "CommandRunner",
    "InstanceContext",
    "InstanceError",
    "LeaseError",
    "StandbyError",
    "ensure_active",
    "ensure_private_repository",
    "handover",
    "init",
    "instance_is_active",
    "pull",
    "push",
    "status",
    "subprocess_runner",
]

CODE_REPOSITORY = "ChardXBT/Runway"
SNAPSHOT_TAG = "snapshot-latest"
MANIFEST_FILE_NAME = "manifest.json"
MANIFEST_FORMAT_VERSION = 1
SOPS_CONFIG_NAME = ".sops.yaml"
TOKENS_FILE = f"{TOKENS_DIR_NAME}/platform-tokens.json"
ASSET_NAMES = (DATABASE_ARCHIVE_NAME, MEDIA_ARCHIVE_NAME, MANIFEST_NAME)
WORKER_POLL_SECONDS = 5.0
DEFAULT_WORKER_WAIT_SECONDS = 300

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
_GITHUB_REMOTE_PATTERN = re.compile(
    r"github\.com[:/](?P<name>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
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
*.zip
*.key
.env
.env.*
browser-profile/
tokens/token.lock
tokens/platform-tokens-*.json
.lease-*.json
"""
# Byte-exact: the Fernet blob and SOPS ciphertext must never be rewritten for line endings.
_GITATTRIBUTES = "* -text\n"
_README = """\
# Runway private instance

Private state for one Runway installation. Keep this repository **private**.
The code lives in the public `ChardXBT/Runway` repository; nothing here may be copied there.

- `instance.toml`: non-secret settings (Runway setting names without the `RUNWAY_` prefix).
- `secrets.enc.env`: SOPS + age encrypted secrets. Edit with `sops edit secrets.enc.env`.
- `tokens/platform-tokens.json`: the Fernet-encrypted token store; its key is in the secrets.
- `lease.json`: the one machine allowed to refresh tokens and publish.
- `manifest.json`: the last snapshot. The database and media are assets of the
  `snapshot-latest` release.

Restore on another machine: clone the code and this repository, install this machine's age
key, set `RUNWAY_INSTANCE_DIR`, then run `runway instance pull` after the active machine ran
`runway instance handover`. See `docs/PRIVATE_INSTANCE.md` in the code repository.
"""
_RELEASE_NOTES = (
    "Rolling private snapshot of this Runway instance: the database (SQLite online backup, "
    "gzip) and every media file it references. Browser profiles, plain keys and "
    "authentication state are never included."
)


class InstanceError(RuntimeError):
    """A ``runway instance`` command refused or failed safely."""


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


def subprocess_runner(
    args: Sequence[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0])
    if executable is None:
        hint = _INSTALL_HINTS.get(args[0], "Install it")
        raise InstanceError(f"'{args[0]}' was not found on PATH. {hint}.")
    return subprocess.run(
        [executable, *args[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


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


def _failure(args: Sequence[str], result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
    reason = safe_error_summary(RuntimeError(lines[-1])) if lines else "no error output"
    shown = " ".join(arg for arg in args[:3] if not os.path.isabs(arg))
    return f"'{shown}' failed (exit {result.returncode}): {reason}"


# --- repositories -----------------------------------------------------------------------


def validate_repository(name: str) -> str:
    value = name.strip()
    if not _REPOSITORY_PATTERN.fullmatch(value) or value.endswith((".git", ".")):
        raise InstanceError(f"{name!r} is not a GitHub repository name in owner/name form")
    return value


def _github_name(remote_url: str) -> str | None:
    match = _GITHUB_REMOTE_PATTERN.search(remote_url.strip())
    return match.group("name") if match else None


def _code_repositories(ctx: InstanceContext) -> set[str]:
    blocked = {CODE_REPOSITORY.casefold()}
    try:
        origin = ctx.git(
            ctx.settings.project_root, "config", "--get", "remote.origin.url", check=False
        )
    except InstanceError:
        return blocked
    name = _github_name(origin.stdout) if origin.returncode == 0 else None
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
    canonical = str(payload.get("nameWithOwner") or name)
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
    return _github_name(result.stdout) if result.returncode == 0 else None


def _require_instance_dir(settings: Settings) -> Path:
    instance_dir = settings.resolved_instance_dir
    if instance_dir is None:
        raise InstanceError(
            "RUNWAY_INSTANCE_DIR is not set. Point it at the private instance clone "
            "(outside this checkout), or run "
            "'runway instance init --repo <owner/name> --dir <path>'"
        )
    return instance_dir


def _prepare(ctx: InstanceContext) -> tuple[Path, str]:
    """The instance clone and its confirmed-private repository, for commands that sync."""
    instance_dir = _require_instance_dir(ctx.settings)
    if not (instance_dir / ".git").exists():
        raise InstanceError(
            f"{instance_dir} is not a clone of the private instance repository; "
            "run 'runway instance init --repo <owner/name>' first"
        )
    origin = _origin_repository(ctx, instance_dir)
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
    return instance_dir, ensure_private_repository(ctx, repository)


# --- git ---------------------------------------------------------------------------------


def _git_sync(ctx: InstanceContext, instance_dir: Path) -> None:
    result = ctx.git(instance_dir, "pull", "--ff-only", "--quiet", check=False)
    if result.returncode != 0:
        raise InstanceError(
            "could not fast-forward the instance clone from its remote "
            f"({_failure(['git', 'pull'], result)}); resolve it with git in {instance_dir}"
        )


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


def _git_push(ctx: InstanceContext, instance_dir: Path) -> None:
    result = ctx.git(instance_dir, "push", "--quiet", "-u", "origin", "HEAD", check=False)
    if result.returncode != 0:
        raise InstanceError(
            "the private repository changed while this command ran "
            f"({_failure(['git', 'push'], result)}); run 'runway instance status --refresh'"
        )


def _publish_lease(ctx: InstanceContext, instance_dir: Path, lease: Lease, message: str) -> None:
    """Write, commit and push the lease; a rejected push undoes the local change.

    A fast-forward-only push is the compare-and-swap: two machines cannot both
    take the lease from the same starting commit.
    """
    existed = (instance_dir / LEASE_FILE_NAME).is_file()
    previous = (instance_dir / LEASE_FILE_NAME).read_bytes() if existed else None
    write_lease(instance_dir, lease)
    if not _commit(ctx, instance_dir, message, [LEASE_FILE_NAME]):
        return
    pushed = ctx.git(instance_dir, "push", "--quiet", "-u", "origin", "HEAD", check=False)
    if pushed.returncode == 0:
        return
    ctx.git(instance_dir, "reset", "--quiet", "--soft", "HEAD~1")
    if previous is None:
        ctx.git(instance_dir, "rm", "--quiet", "--cached", "--", LEASE_FILE_NAME)
        (instance_dir / LEASE_FILE_NAME).unlink(missing_ok=True)
    else:
        ctx.git(instance_dir, "reset", "--quiet", "HEAD", "--", LEASE_FILE_NAME)
        (instance_dir / LEASE_FILE_NAME).write_bytes(previous)
    raise InstanceError(
        "could not publish the lease: another machine changed the private repository "
        f"meanwhile ({_failure(['git', 'push'], pushed)}). Run 'runway instance status --refresh'."
    )


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
            raise InstanceError(
                "the worker is still publishing (data/runtime/worker.busy is present); "
                "try again when the publish has finished"
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


def _encrypt_secrets(
    ctx: InstanceContext, destination: Path, recipients: Sequence[str], values: dict[str, str]
) -> None:
    """Create ``secrets.enc.env`` with SOPS.

    SOPS reads its plaintext from a file, so it exists for the duration of one
    ``sops`` call inside a private temporary directory, then is deleted.
    """
    plaintext = "".join(f"{key}={value}\n" for key, value in values.items())
    with tempfile.TemporaryDirectory(prefix="runway-instance-secrets-") as temporary:
        source = Path(temporary) / "secrets.env"
        fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(plaintext)
        try:
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
        instance_dir.parent.mkdir(parents=True, exist_ok=True)
        ctx.run(["gh", "repo", "clone", repo, str(instance_dir)])
    elif not (instance_dir / ".git").exists():
        raise InstanceError(
            f"{instance_dir} exists and is not a git clone; choose an empty or new directory"
        )
    origin = _origin_repository(ctx, instance_dir)
    if origin is None or origin.casefold() != repo.casefold():
        raise InstanceError(
            f"{instance_dir} is a clone of {origin or 'an unknown remote'}, not {repo}"
        )

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
        "# Non-secret Runway settings for this instance: setting names without RUNWAY_.\n"
        "# Secrets go in secrets.enc.env (sops edit secrets.enc.env), never here.\n"
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
        _encrypt_secrets(ctx, secrets_file, recipients or [recipient], values)
        created.append(SECRETS_FILE_NAME)
        if legacy_key is not None:
            next_steps.append(
                "The token key was moved into secrets.enc.env. Once Runway runs against the "
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

    committed = _commit(ctx, instance_dir, "Initialize the Runway instance layout", TRACKED_FILES)
    if committed:
        _git_push(ctx, instance_dir)

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
        _git_sync(ctx, instance_dir)
    lease = read_lease(instance_dir)
    now = ctx.now()
    report["lease"] = {
        "description": describe_lease(lease),
        "holder": lease.holder if lease else None,
        "stale": lease.is_stale(now) if lease else False,
    }
    report["active"] = instance_is_active(settings)
    report["mode"] = "active" if report["active"] else "standby"

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
        created_at = manifest.get("created_at")
        if settings.database_path.is_file() and isinstance(created_at, str):
            modified = datetime.fromtimestamp(settings.database_path.stat().st_mtime, UTC)
            if modified > datetime.fromisoformat(created_at):
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


def _upload_snapshot(ctx: InstanceContext, repository: str, workdir: Path) -> dict[str, Any]:
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
                "Runway instance snapshot",
                "--notes",
                _RELEASE_NOTES,
                "--prerelease",
            ]
        )
    files = [workdir / name for name in ASSET_NAMES]
    ctx.run(
        [
            "gh",
            "release",
            "upload",
            SNAPSHOT_TAG,
            *(str(path) for path in files),
            "--clobber",
            "--repo",
            repository,
        ]
    )
    assets = {path.name: _file_entry(path) for path in files}
    release_info = ctx.run(["gh", "api", f"repos/{repository}/releases/tags/{SNAPSHOT_TAG}"])
    try:
        uploaded = {
            str(asset["name"]): asset for asset in json.loads(release_info.stdout).get("assets", [])
        }
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
        raise InstanceError("gh returned unreadable release data") from exc
    for name, entry in assets.items():
        asset = uploaded.get(name)
        if entry is None or asset is None or int(asset.get("size", -1)) != entry["bytes"]:
            raise InstanceError(f"uploaded snapshot asset {name} is missing or has the wrong size")
        digest = asset.get("digest")
        if digest and digest != f"sha256:{entry['sha256']}":
            raise InstanceError(f"uploaded snapshot asset {name} has the wrong digest")
    return assets


def push(
    ctx: InstanceContext, *, wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS
) -> dict[str, Any]:
    """Snapshot the database and media, upload them, and commit the small files."""
    settings = ctx.settings
    instance_dir, repository = _prepare(ctx)
    _git_sync(ctx, instance_dir)
    me = ctx.machine
    lease = read_lease(instance_dir)
    if lease is None or lease.holder != me:
        raise LeaseError(
            f"only the machine holding the lease may push ({describe_lease(lease)}). "
            "To make this machine active, run 'runway instance pull'."
        )
    wait_for_idle_worker(ctx, wait_seconds)
    database = settings.database_path
    if not database.is_file():
        raise InstanceError(f"there is no database at {database} to snapshot")
    commit, branch = _code_revision(ctx)
    with tempfile.TemporaryDirectory(prefix="runway-instance-push-") as temporary:
        workdir = Path(temporary)
        snapshot = create_release(
            source_database=database,
            data_root=settings.resolved_data_dir,
            output_directory=workdir,
            commit_sha=commit,
            branch=branch,
        )
        assets = _upload_snapshot(ctx, repository, workdir)
    now = ctx.now()
    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "created_at": now.isoformat(),
        "machine": me,
        "repository": repository,
        "code": {"repository": CODE_REPOSITORY, "commit": snapshot["commit_sha"], "branch": branch},
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
        "release": {"tag": SNAPSHOT_TAG, "assets": assets},
        "files": {
            name: entry
            for name in (TOKENS_FILE, SECRETS_FILE_NAME, INSTANCE_TOML_NAME)
            if (entry := _file_entry(instance_dir / name)) is not None
        },
    }
    _write_json(instance_dir / MANIFEST_FILE_NAME, manifest)
    write_lease(instance_dir, acquire(lease, me, now, take_stale=False))
    _commit(ctx, instance_dir, f"Snapshot from {me}", TRACKED_FILES)
    _git_push(ctx, instance_dir)
    return {
        "status": "pushed",
        "repository": repository,
        "machine": me,
        "alembic_revision": manifest["alembic_revision"],
        "code_commit": manifest["code"]["commit"],
        "assets": {name: entry["bytes"] for name, entry in assets.items() if entry},
    }


def handover(
    ctx: InstanceContext, *, wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS
) -> dict[str, Any]:
    """Push, then release the lease so another machine can pull."""
    result = push(ctx, wait_seconds=wait_seconds)
    instance_dir = _require_instance_dir(ctx.settings)
    released = release(read_lease(instance_dir), ctx.machine, ctx.now())
    _publish_lease(ctx, instance_dir, released, f"Hand over the lease from {ctx.machine}")
    return {**result, "status": "handed over", "mode": "standby"}


def _download_snapshot(
    ctx: InstanceContext, repository: str, manifest: dict[str, Any], destination: Path
) -> None:
    ctx.run(
        [
            "gh",
            "release",
            "download",
            SNAPSHOT_TAG,
            "--repo",
            repository,
            *(part for name in ASSET_NAMES for part in ("--pattern", name)),
            "--dir",
            str(destination),
            "--clobber",
        ]
    )
    expected = manifest.get("release", {}).get("assets", {})
    for name in ASSET_NAMES:
        if _file_entry(destination / name) != expected.get(name):
            raise InstanceError(
                f"downloaded snapshot asset {name} does not match manifest.json; "
                "the release and the instance repository are out of step"
            )


def _install_restored(data_dir: Path, staging: Path, stamp: str) -> Path | None:
    """Move the verified restore into the data directory, backing up what it replaces."""
    backup_dir = data_dir / "backups" / f"pre-instance-pull-{stamp}"
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
    wait_seconds: float = DEFAULT_WORKER_WAIT_SECONDS,
) -> dict[str, Any]:
    """Take the lease and restore the latest snapshot into this machine's data directory."""
    settings = ctx.settings
    instance_dir, repository = _prepare(ctx)
    _git_sync(ctx, instance_dir)
    me = ctx.machine
    now = ctx.now()
    lease = read_lease(instance_dir)
    new_lease = acquire(lease, me, now, take_stale=take)
    manifest = read_manifest(instance_dir)
    if manifest is None:
        raise InstanceError("no snapshot has been pushed yet; run 'runway instance push' first")
    revision = manifest.get("alembic_revision")
    ensure_snapshot_revision_supported(settings, revision if isinstance(revision, str) else None)
    wait_for_idle_worker(ctx, wait_seconds)
    _publish_lease(ctx, instance_dir, new_lease, f"Take the lease on {me}")

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
        backup = _install_restored(data_dir, Path(staging), now.strftime("%Y%m%dT%H%M%SZ"))
    return {
        "status": "pulled",
        "repository": repository,
        "machine": me,
        "mode": "active",
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
