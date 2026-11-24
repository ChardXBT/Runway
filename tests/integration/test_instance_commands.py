"""``runway instance`` end to end against a fake GitHub.

``gh`` is never executed: :class:`FakeGitHub` answers every ``gh`` call, keeps
release assets in a temporary directory and backs each "GitHub" repository
with a local bare repository. ``git`` is real, but every
``https://github.com/<name>.git`` argument is translated to that bare
repository, a private global git config is used, and ``GIT_ALLOW_PROTOCOL=file``
makes any other transport fail, so no test can reach the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from typer.testing import CliRunner

import runway.cli.instance as instance_cli
import runway.config.instance as instance_config
from runway.cli.main import app
from runway.config import Settings
from runway.db import Database, initialize_database
from runway.db.models import ConnectedAccount, MediaAsset
from runway.locks import FileLockTimeout, exclusive_file_lock
from runway.operations import instance as ops
from runway.operations import instance_lease
from runway.operations.instance import InstanceContext, InstanceError
from runway.operations.instance_lease import (
    LeaseError,
    StandbyError,
    instance_is_active,
    read_lease,
)
from runway.operations.runtime_files import api_lock_path, worker_lock_path
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import FileTokenStore

PRIVATE_REPO = "owner/runway-instance"
PUBLIC_REPO = "owner/public-showcase"
FAKE_CIPHER_PREFIX = "fake-sops-ciphertext:"


def _completed(
    args: Sequence[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), code, stdout=stdout, stderr=stderr)


def _files_before_options(args: list[str], start: int) -> list[str]:
    files: list[str] = []
    for value in args[start:]:
        if value.startswith("--"):
            break
        files.append(value)
    return files


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.sops_inputs: list[Path] = []
        self.reject_pushes = False
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        # Fixed values layered over the environment _git_environment builds per call.
        self.git_env_overrides = {
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Runway Test",
            "GIT_AUTHOR_EMAIL": "runway-test@example.invalid",
            "GIT_COMMITTER_NAME": "Runway Test",
            "GIT_COMMITTER_EMAIL": "runway-test@example.invalid",
        }

    def add_repo(self, name: str, visibility: str = "PRIVATE") -> Path:
        bare = self.root / "remotes" / f"{name.replace('/', '__')}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        self._real_git(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)])
        self.repos[name.casefold()] = {"name": name, "visibility": visibility, "bare": bare}
        return bare

    def bare(self, name: str) -> Path:
        return Path(self.repos[name.casefold()]["bare"])

    def release_dir(self, repository: str, tag: str) -> Path:
        return self.root / "releases" / repository.replace("/", "__") / tag

    def releases(self, repository: str) -> list[str]:
        base = self.root / "releases" / repository.replace("/", "__")
        return sorted(path.name for path in base.iterdir()) if base.is_dir() else []

    def uploads(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if call[:3] in (["gh", "release", "upload"], ["gh", "release", "create"])
        ]

    def _git_environment(self) -> dict[str, str]:
        """os.environ without GIT_CONFIG_* injection, like the production runner."""
        env = {
            name: value
            for name, value in os.environ.items()
            if name != "GIT_CONFIG"
            and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            and name not in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
        }
        env.update(self.git_env_overrides)
        return env

    def _untranslate(self, text: str) -> str:
        """Show the bare repositories the way the code addressed them: as URLs."""
        for repo in self.repos.values():
            url = f"https://github.com/{repo['name']}.git"
            bare = Path(repo["bare"])
            for form in (str(bare), bare.as_posix()):
                text = text.replace(form, url)
        return text

    def _real_git(
        self, args: Sequence[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=self._git_environment(),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        return _completed(
            args,
            result.returncode,
            self._untranslate(result.stdout),
            self._untranslate(result.stderr),
        )

    def _translate(self, value: str) -> str:
        for repo in self.repos.values():
            if value.casefold() == f"https://github.com/{repo['name']}.git".casefold():
                return str(repo["bare"])
        return value

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[0] == "git":
            if self.reject_pushes and "push" in args and "--dry-run" not in args:
                return _completed(args, 1, stderr="! [rejected] HEAD -> main (fetch first)")
            return self._real_git([self._translate(arg) for arg in args], cwd)
        if args[0] == "gh":
            return self._gh(args)
        if args[0] == "sops":
            if "--encrypt" in args:
                self.sops_inputs.append(Path(args[-1]))
            return fake_sops(args)
        if args[0] == "age-keygen":
            return fake_age_keygen(args)
        if args[0] == "icacls":
            return subprocess.run(args, capture_output=True, text=True, check=False)
        raise AssertionError(f"unexpected command {args[0]}")

    def _repo(self, name: str) -> dict[str, Any] | None:
        return self.repos.get(name.casefold())

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1] == "api":
            return _gh_api(self, args)
        command = args[1:3]
        if command == ["repo", "view"]:
            repo = self._repo(args[3])
            if repo is None:
                return _completed(args, 1, stderr="GraphQL: Could not resolve to a Repository")
            payload = {"nameWithOwner": repo["name"], "visibility": repo["visibility"]}
            return _completed(args, stdout=json.dumps(payload))
        if command == ["repo", "clone"]:
            repo = self._repo(args[3])
            assert repo is not None
            target = Path(args[4])
            result = self._real_git(["git", "clone", "--quiet", str(repo["bare"]), str(target)])
            assert result.returncode == 0, result.stderr
            url = f"https://github.com/{repo['name']}.git"
            configured = self._real_git(
                ["git", "-C", str(target), "remote", "set-url", "origin", url]
            )
            assert configured.returncode == 0, configured.stderr
            return _completed(args)
        repo = self._repo(_option(args, "--repo"))
        assert repo is not None, args
        if command == ["release", "list"]:
            tags = [{"tagName": tag} for tag in self.releases(repo["name"])]
            return _completed(args, stdout=json.dumps(tags))
        release_dir = self.release_dir(repo["name"], args[3])
        if command == ["release", "view"]:
            return _completed(args, 0 if release_dir.is_dir() else 1, stdout="{}")
        if command == ["release", "create"]:
            assert repo["visibility"] == "PRIVATE", "uploaded to a non-private repository"
            release_dir.mkdir(parents=True)
            for value in _files_before_options(args, 4):
                shutil.copy2(value, release_dir / Path(value).name)
            return _completed(args)
        if command == ["release", "upload"]:
            assert repo["visibility"] == "PRIVATE", "uploaded to a non-private repository"
            assert release_dir.is_dir() and "--clobber" in args
            for value in _files_before_options(args, 4):
                shutil.copy2(value, release_dir / Path(value).name)
            return _completed(args)
        if command == ["release", "delete"]:
            assert "--yes" in args
            shutil.rmtree(release_dir)
            return _completed(args)
        if command == ["release", "download"]:
            destination = Path(_option(args, "--dir"))
            patterns = [args[i + 1] for i, value in enumerate(args) if value == "--pattern"]
            for name in patterns:
                shutil.copy2(release_dir / name, destination / name)
            return _completed(args)
        raise AssertionError(f"unexpected gh call {args}")


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _gh_api(fake: FakeGitHub, args: list[str]) -> subprocess.CompletedProcess[str]:
    _, owner, name, _, _, tag = args[2].split("/")
    release_dir = fake.release_dir(f"{owner}/{name}", tag)
    assets = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(release_dir.iterdir())
    ]
    return _completed(args, stdout=json.dumps({"tag_name": tag, "assets": assets}))


def fake_sops(args: list[str]) -> subprocess.CompletedProcess[str]:
    if "--encrypt" in args:
        assert args[args.index("--age") + 1].startswith("age1")
        plaintext = Path(args[-1]).read_bytes()
        encoded = base64.b64encode(plaintext).decode("ascii")
        Path(_option(args, "--output")).write_text(FAKE_CIPHER_PREFIX + encoded + "\n")
        return _completed(args)
    assert args[1:6] == ["--decrypt", "--input-type", "dotenv", "--output-type", "dotenv"]
    text = Path(args[-1]).read_text(encoding="utf-8").strip()
    assert text.startswith(FAKE_CIPHER_PREFIX)
    decoded = base64.b64decode(text[len(FAKE_CIPHER_PREFIX) :]).decode("utf-8")
    return _completed(args, stdout=decoded)


def fake_age_keygen(args: list[str]) -> subprocess.CompletedProcess[str]:
    path = Path(args[-1])
    if args[1] == "-o":
        assert not path.exists()
        path.write_text(f"fake age identity {os.urandom(8).hex()}\n", encoding="utf-8")
        return _completed(args, stderr="Public key: age1...")
    assert args[1] == "-y"
    digest = hashlib.sha512(path.read_bytes()).hexdigest()
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    recipient = "age1" + "".join(
        alphabet[int(digest[i : i + 2], 16) % 36] for i in range(0, 116, 2)
    )
    return _completed(args, stdout=recipient + "\n")


class Clock:
    """Real time plus an offset: file modification times stay comparable."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, **delta: float) -> None:
        self.offset += timedelta(**delta)


@dataclass
class Machine:
    name: str
    root: Path
    fake: FakeGitHub
    clock: Clock

    @property
    def instance_dir(self) -> Path:
        return self.root / "runway-instance"

    @property
    def age_key(self) -> Path:
        return self.root / "profile" / "sops" / "age" / "keys.txt"

    def settings(self, **overrides: Any) -> Settings:
        return Settings(
            data_dir=self.root / "data",
            instance_dir=self.instance_dir,
            machine_id=self.name,
            platform_provider="mock",
            **overrides,
        )

    def context(self, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> InstanceContext:
        # Each machine has its own age identity, as a user profile would.
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(self.age_key))
        return InstanceContext(
            settings=self.settings(**overrides),
            runner=self.fake,
            now=self.clock.now,
            sleep=lambda _seconds: None,
        )


@pytest.fixture()
def fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    (tmp_path / "github").mkdir()
    fake = FakeGitHub(tmp_path / "github")
    fake.add_repo(PRIVATE_REPO)
    fake.add_repo(PUBLIC_REPO, visibility="PUBLIC")
    # Settings decrypt secrets.enc.env through the same fake sops, and the remote lease
    # check (which has no InstanceContext) goes through the same fake GitHub.
    monkeypatch.setattr(instance_config, "sops_runner", lambda args: fake_sops(list(args)))
    monkeypatch.setattr(ops, "default_runner", fake)
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "profile" / "machine-id"))
    for name in ("RUNWAY_INSTANCE_DIR", "RUNWAY_INSTANCE_REPOSITORY", "RUNWAY_MACHINE_ID"):
        monkeypatch.delenv(name, raising=False)
    yield fake


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def _machine(tmp_path: Path, name: str, fake: FakeGitHub, clock: Clock) -> Machine:
    root = tmp_path / name
    root.mkdir()
    return Machine(name=name, root=root, fake=fake, clock=clock)


def _seed_runway(settings: Settings) -> tuple[str, TokenSet]:
    """A fixture database with a connected account, a media file and its token."""
    database = initialize_database(settings)
    media = settings.resolved_data_dir / "media" / "approved" / "post.jpg"
    media.write_bytes(b"fixture-image-bytes")
    token = TokenSet(
        access_token="fixture-access-token",
        refresh_token="fixture-refresh-token",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    store = FileTokenStore.from_settings(settings)
    token_ref = store.new_token_ref()
    store.store(token_ref, token)
    with database.session() as session:
        session.add(
            ConnectedAccount(
                platform="youtube",
                external_account_id="UC-fixture",
                handle="fixture",
                status="connected",
                token_ref=token_ref,
            )
        )
        session.add(
            MediaAsset(
                kind="approved",
                local_path="media/approved/post.jpg",
                mime_type="image/jpeg",
                width=640,
                height=480,
                file_size=media.stat().st_size,
                sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
                perceptual_hash="0" * 16,
            )
        )
    database.engine.dispose()
    return token_ref, token


def _remote_file(fake: FakeGitHub, name: str) -> str:
    bare = fake.repos[PRIVATE_REPO.casefold()]["bare"]
    result = fake._real_git(["git", "--git-dir", str(bare), "show", f"main:{name}"])
    assert result.returncode == 0, result.stderr
    return result.stdout


# --- init --------------------------------------------------------------------------------


def test_init_against_a_public_repository_refuses(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)

    with pytest.raises(InstanceError, match="public, not private"):
        ops.init(machine.context(monkeypatch), PUBLIC_REPO)

    assert not machine.instance_dir.exists()
    assert not machine.age_key.exists()
    assert [call[:3] for call in fake_github.calls if call[0] == "gh"] == [["gh", "repo", "view"]]


def test_init_refuses_the_code_repository_even_when_private_or_renamed(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    fake_github.add_repo("ChardXBT/Runway", visibility="PRIVATE")
    with pytest.raises(InstanceError, match="code repository"):
        ops.init(machine.context(monkeypatch), "chardxbt/runway")
    assert not any(call[:2] == ["gh", "repo"] for call in fake_github.calls)

    # A redirecting old name resolves to the code repository: refused after gh answers.
    fake_github.repos["owner/old-name"] = {
        **fake_github.repos["chardxbt/runway"],
        "name": "ChardXBT/Runway",
    }
    with pytest.raises(InstanceError, match="code repository"):
        ops.init(machine.context(monkeypatch), "owner/old-name")
    assert not machine.instance_dir.exists()


@pytest.mark.parametrize("name", ["runway-instance", "--repo=evil/x", "owner/../x", "a/b c"])
def test_init_rejects_malformed_repository_names(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    with pytest.raises(InstanceError, match="owner/name"):
        ops.init(machine.context(monkeypatch), name)
    assert fake_github.calls == []


def test_init_creates_the_layout_and_migrates_the_plain_key_into_encrypted_secrets(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    legacy = FileTokenStore(machine.root / "data" / "secrets")
    legacy.store("legacy-ref", TokenSet(access_token="legacy-access"))
    legacy_key = legacy.key_path.read_text().strip()
    fixture_secret = "fixture-client-secret-value-0123"

    result = ops.init(
        machine.context(
            monkeypatch,
            youtube_client_id="fixture-client-id",
            youtube_client_secret=fixture_secret,
        ),
        PRIVATE_REPO,
    )

    instance_dir = machine.instance_dir
    for name in ops.TRACKED_FILES[:-1]:
        assert (instance_dir / name).is_file(), name
    assert result["imported_token_store"] is True
    assert result["age_key_created"] is True
    assert result["age_recipient"] in (instance_dir / ".sops.yaml").read_text()
    secrets_bytes = (instance_dir / "secrets.enc.env").read_bytes()
    assert legacy_key.encode() not in secrets_bytes
    assert fixture_secret.encode() not in secrets_bytes
    lease = read_lease(instance_dir)
    assert lease is not None and lease.holder == "machine-a"
    # Committed and pushed to the private remote.
    assert "owner/runway-instance" in _remote_file(fake_github, "instance.toml")
    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] == "machine-a"
    # Nothing secret on any command line.
    for call in fake_github.calls:
        assert legacy_key not in " ".join(call)
        assert fixture_secret not in " ".join(call)

    settings = machine.settings()
    assert settings.instance_repository == PRIVATE_REPO
    assert settings.token_key is not None
    assert settings.token_key.get_secret_value() == legacy_key
    assert settings.youtube_client_secret == fixture_secret
    token = FileTokenStore.from_settings(settings).load("legacy-ref")
    assert token is not None and token.access_token == "legacy-access"
    # The only plain key file is the pre-existing legacy one, which init leaves for the owner.
    assert [path for path in tmp_path.rglob("*.key")] == [legacy.key_path]
    assert any("token.key" in step for step in result["next_steps"])


# --- push / pull / handover ----------------------------------------------------------------


def test_push_then_pull_on_a_clean_machine_yields_a_working_instance(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)

    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    token_ref, token = _seed_runway(machine_a.settings())
    pushed = ops.push(machine_a.context(monkeypatch))
    assert pushed["status"] == "pushed"
    assert set(pushed["assets"]) == set(ops.ASSET_NAMES)
    manifest = json.loads(_remote_file(fake_github, "manifest.json"))
    assert manifest["alembic_revision"] in ops.checkout_revisions(machine_a.settings())
    assert manifest["code"]["repository"] == "ChardXBT/Runway"
    assert set(manifest["files"]) >= {"tokens/platform-tokens.json", "secrets.enc.env"}
    assert all(
        entry["sha256"] and entry["bytes"] for entry in manifest["release"]["assets"].values()
    )

    # Machine B joins: it clones the instance but stays in standby while A holds the lease.
    joined = ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    assert any("standby" in step for step in joined["next_steps"])
    assert not instance_is_active(machine_b.settings())
    with pytest.raises(LeaseError, match="machine-a holds the lease"):
        ops.pull(machine_b.context(monkeypatch))
    assert not (machine_b.root / "data" / "runway.db").exists()

    clock.advance(minutes=5)
    handed = ops.handover(machine_a.context(monkeypatch))
    assert handed["mode"] == "standby"
    assert not instance_is_active(machine_a.settings())

    clock.advance(minutes=5)
    pulled = ops.pull(machine_b.context(monkeypatch))
    assert pulled["status"] == "pulled"
    assert pulled["replaced_files_backup"] is None

    settings_b = machine_b.settings()
    assert instance_is_active(settings_b)
    assert (settings_b.resolved_data_dir / "media" / "approved" / "post.jpg").read_bytes() == (
        b"fixture-image-bytes"
    )
    database = Database(settings_b)
    try:
        with database.session() as session:
            account = session.scalar(select(ConnectedAccount))
            assert account is not None
            assert account.status == "connected"
            assert account.token_ref == token_ref
    finally:
        database.engine.dispose()
    restored = FileTokenStore.from_settings(settings_b).load(token_ref)
    assert restored == token
    # Runway starts against the restored instance (migrations are already at head).
    initialize_database(settings_b).engine.dispose()

    # A is now the standby machine: it cannot refresh tokens.
    settings_a = machine_a.settings()
    registry_a = ProviderRegistry(Database(settings_a), settings_a)
    expiring = token.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(minutes=1)})
    with pytest.raises(StandbyError):
        registry_a.token_store.store(token_ref, expiring)
    assert settings_a.token_key is not None
    unguarded = FileTokenStore(
        settings_a.token_store_dir,
        key=settings_a.token_key.get_secret_value().encode(),
        key_file=False,
    )
    unguarded.store(token_ref, expiring)
    with pytest.raises(StandbyError):
        registry_a.fresh_token(Platform.youtube, token_ref)
    registry_a.database.engine.dispose()

    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] == "machine-b"
    for path in (machine_a.root, machine_b.root):
        assert not list(path.rglob("*.key")), "no plain token key on disk"


def test_pull_backs_up_the_database_it_replaces(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.handover(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    stale = machine_b.root / "data" / "runway.db"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"older local database")

    pulled = ops.pull(machine_b.context(monkeypatch))

    backup = Path(pulled["replaced_files_backup"])
    assert (backup / "runway.db").read_bytes() == b"older local database"
    assert stale.read_bytes() != b"older local database"


def test_pull_refuses_a_snapshot_newer_than_the_code_checkout(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.handover(machine_a.context(monkeypatch))
    # Pretend A ran newer code: its manifest names a migration this checkout lacks.
    manifest_path = machine_a.instance_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["alembic_revision"] = "9999_from_the_future"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bare = str(fake_github.bare(PRIVATE_REPO))
    for git_args in (["commit", "-q", "-am", "newer snapshot"], ["push", "-q", bare, "HEAD:main"]):
        result = fake_github._real_git(["git", "-C", str(machine_a.instance_dir), *git_args])
        assert result.returncode == 0, result.stderr
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)

    with pytest.raises(InstanceError, match="Update the code first"):
        ops.pull(machine_b.context(monkeypatch))

    # The refusal comes before the lease is taken or anything is downloaded.
    lease = read_lease(machine_b.instance_dir)
    assert lease is not None and lease.holder is None
    assert not any(call[:3] == ["gh", "release", "download"] for call in fake_github.calls)
    assert not (machine_b.root / "data" / "runway.db").exists()


def test_a_stale_lease_needs_take(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.push(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)

    clock.advance(hours=24, minutes=1)  # machine A went silent
    with pytest.raises(LeaseError, match="--take"):
        ops.pull(machine_b.context(monkeypatch))
    taken = ops.pull(
        machine_b.context(monkeypatch), take=True, i_know_the_other_machine_is_off=True
    )
    assert taken["status"] == "pulled"
    assert instance_is_active(machine_b.settings())
    # A comes back: it is in standby and cannot push over B's lease.
    with pytest.raises(LeaseError, match="only the machine holding the lease"):
        ops.push(machine_a.context(monkeypatch))


def test_push_waits_for_an_idle_worker_and_gives_up_safely(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    marker = instance_lease.mark_worker_busy(machine.settings(), "youtube_publish")

    sleeps: list[float] = []

    def sleep_then_finish(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds=seconds)
        if len(sleeps) == 2:
            marker.unlink()

    context = machine.context(monkeypatch)
    context.sleep = sleep_then_finish
    assert ops.push(context, wait_seconds=60)["status"] == "pushed"
    assert sleeps == [ops.WORKER_POLL_SECONDS, ops.WORKER_POLL_SECONDS]

    uploads_before = len(fake_github.uploads())
    # The legacy single marker file is honoured too.
    legacy = machine.root / "data" / "runtime" / "worker.busy"
    legacy.write_text("publishing", encoding="utf-8")
    context = machine.context(monkeypatch)
    context.sleep = lambda seconds: clock.advance(seconds=seconds)
    with pytest.raises(InstanceError, match="still running"):
        ops.push(context, wait_seconds=12)
    with pytest.raises(InstanceError, match="still running"):
        ops.handover(context, wait_seconds=0)
    assert len(fake_github.uploads()) == uploads_before
    lease = read_lease(machine.instance_dir)
    assert lease is not None and lease.holder == "machine-a"


def test_push_refuses_once_the_instance_repository_is_public(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    fake_github.repos[PRIVATE_REPO.casefold()]["visibility"] = "PUBLIC"

    with pytest.raises(InstanceError, match="never go to a public repository"):
        ops.push(machine.context(monkeypatch))
    assert fake_github.uploads() == []


def test_status_reports_lease_snapshot_and_drift(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    report = ops.status(machine.context(monkeypatch))
    assert report["mode"] == "active"
    assert report["last_snapshot"] is None

    token_ref, token = _seed_runway(machine.settings())
    ops.push(machine.context(monkeypatch))
    report = ops.status(machine.context(monkeypatch))
    assert report["drift"] == []
    assert report["lease"]["holder"] == "machine-a"
    assert report["last_snapshot"]["machine"] == "machine-a"

    FileTokenStore.from_settings(machine.settings()).store(token_ref, token)
    report = ops.status(machine.context(monkeypatch))
    assert "the token store changed since the snapshot" in report["drift"]


def test_cli_init_refuses_a_public_repository_without_calling_real_gh(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(machine.age_key))
    monkeypatch.setattr(instance_cli, "get_settings", machine.settings)
    monkeypatch.setattr(
        instance_cli,
        "InstanceContext",
        lambda settings: InstanceContext(settings=settings, runner=fake_github, now=clock.now),
    )

    result = CliRunner().invoke(app, ["instance", "init", "--repo", PUBLIC_REPO])

    assert result.exit_code == 1
    assert "Refused" in result.output and "not private" in result.output
    assert not machine.instance_dir.exists()


def test_default_runner_names_missing_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(InstanceError, match="GitHub.cli"):
        ops.subprocess_runner(["gh", "--version"])
    with pytest.raises(InstanceError, match="FiloSottile.age"):
        ops.subprocess_runner(["age-keygen", "--version"])


def test_fernet_key_generated_by_init_is_valid(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    key = machine.settings().token_key
    assert key is not None
    Fernet(key.get_secret_value().encode())


# --- review fixes -------------------------------------------------------------------------


def _git_in(fake: FakeGitHub, directory: Path, *args: str) -> None:
    result = fake._real_git(["git", "-C", str(directory), *args])
    assert result.returncode == 0, result.stderr


def _remote_head(fake: FakeGitHub, name: str) -> str:
    result = fake._real_git(["git", "--git-dir", str(fake.bare(name)), "rev-parse", "main"])
    return result.stdout.strip() if result.returncode == 0 else ""


@pytest.mark.parametrize(
    ("git_args", "reason"),
    [
        # An SSH host alias hides where the data goes.
        (["remote", "set-url", "origin", "git@github-chard:owner/runway-instance.git"], "origin"),
        # A port-443 SSH URL and a local path are not recognized destinations either.
        (
            [
                "remote",
                "set-url",
                "origin",
                "ssh://git@ssh.github.com:443/owner/runway-instance.git",
            ],
            "origin",
        ),
        (["remote", "set-url", "origin", "C:/somewhere/runway-instance"], "origin"),
        # origin looks right but its push URL is an SSH host alias nobody can verify.
        (
            ["config", "remote.origin.pushurl", "git@github-chard:owner/runway-instance.git"],
            "pushes to",
        ),
        # origin looks right but pushes to the public repository.
        (["config", "remote.origin.pushurl", f"https://github.com/{PUBLIC_REPO}.git"], "pushes to"),
        # A rewrite rule sends the private URL to the public repository.
        (
            [
                "config",
                f"url.https://github.com/{PUBLIC_REPO}.git.insteadOf",
                f"https://github.com/{PRIVATE_REPO}.git",
            ],
            "pushes to|insteadOf",
        ),
        (
            [
                "config",
                f"url.https://github.com/{PUBLIC_REPO}.git.pushInsteadOf",
                f"https://github.com/{PRIVATE_REPO}.git",
            ],
            "pushes to|insteadOf",
        ),
    ],
)
def test_push_refuses_any_destination_other_than_the_verified_repository(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    git_args: list[str],
    reason: str,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    private_head = _remote_head(fake_github, PRIVATE_REPO)
    _git_in(fake_github, machine.instance_dir, *git_args)

    with pytest.raises(InstanceError, match=f"refused.*({reason})"):
        ops.push(machine.context(monkeypatch))

    assert fake_github.uploads() == []
    assert _remote_head(fake_github, PUBLIC_REPO) == ""  # nothing reached the public repo
    assert _remote_head(fake_github, PRIVATE_REPO) == private_head


def test_push_refuses_when_any_of_several_push_urls_is_elsewhere(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    private_head = _remote_head(fake_github, PRIVATE_REPO)
    # The first push URL is the private repository; a second one (git pushes to every
    # pushurl) points at the public repository.
    private_url = f"https://github.com/{PRIVATE_REPO}.git"
    _git_in(
        fake_github, machine.instance_dir, "config", "--add", "remote.origin.pushurl", private_url
    )
    public_url = f"https://github.com/{PUBLIC_REPO}.git"
    _git_in(
        fake_github, machine.instance_dir, "config", "--add", "remote.origin.pushurl", public_url
    )

    with pytest.raises(InstanceError, match=f"refused.*pushes to {PUBLIC_REPO}"):
        ops.push(machine.context(monkeypatch))

    assert fake_github.uploads() == []
    assert _remote_head(fake_github, PUBLIC_REPO) == ""
    assert _remote_head(fake_github, PRIVATE_REPO) == private_head


@pytest.mark.parametrize("rule", ["insteadOf", "pushInsteadOf"])
def test_push_refuses_a_rewrite_of_the_verified_url_even_when_origin_differs(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    rule: str,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    private_head = _remote_head(fake_github, PRIVATE_REPO)
    # origin uses the SSH form, so its own (unrewritten) URL looks right; the push names
    # the HTTPS URL, which the rule would silently send to the public repository.
    _git_in(
        fake_github,
        machine.instance_dir,
        "remote",
        "set-url",
        "origin",
        f"git@github.com:{PRIVATE_REPO}.git",
    )
    _git_in(
        fake_github,
        machine.instance_dir,
        "config",
        f"url.https://github.com/{PUBLIC_REPO}.git.{rule}",
        f"https://github.com/{PRIVATE_REPO}.git",
    )

    with pytest.raises(InstanceError, match="refused.*insteadOf"):
        ops.push(machine.context(monkeypatch))

    assert fake_github.uploads() == []
    assert _remote_head(fake_github, PUBLIC_REPO) == ""
    assert _remote_head(fake_github, PRIVATE_REPO) == private_head


def test_every_transfer_names_the_verified_url_and_uses_gh_credentials(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    ops.push(machine.context(monkeypatch))

    transfers = [
        call for call in fake_github.calls if call[0] == "git" and ({"push", "fetch"} & set(call))
    ]
    assert transfers
    for call in transfers:
        assert f"https://github.com/{PRIVATE_REPO}.git" in call, call
        assert "origin" not in call, call
        assert "credential.helper=!gh auth git-credential" in call, call


def test_the_active_machine_renews_its_heartbeat_and_blocks_a_take(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.push(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)

    clock.advance(hours=1)
    assert ops.heartbeat(machine_a.context(monkeypatch))["status"] == "current"
    for _ in range(5):  # Runway runs on A for 30 hours without a push
        clock.advance(hours=6)
        assert ops.heartbeat(machine_a.context(monkeypatch))["status"] == "renewed"
    remote = json.loads(_remote_file(fake_github, "lease.json"))
    assert remote["holder"] == "machine-a"
    assert datetime.fromisoformat(remote["heartbeat_at"]) > clock.now() - timedelta(hours=7)

    # More than 24 hours since A's last push, but the remote heartbeat is fresh.
    with pytest.raises(LeaseError, match="handover"):
        ops.pull(machine_b.context(monkeypatch), take=True)
    assert ops.heartbeat(machine_b.context(monkeypatch))["status"] == "standby"


def test_take_is_judged_by_the_remote_lease_not_the_local_copy(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.push(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    clock.advance(hours=25)
    # B's clone is out of date (it still shows A's old heartbeat); A renews remotely.
    ops.heartbeat(machine_a.context(monkeypatch))
    old = json.loads((machine_b.instance_dir / "lease.json").read_text())
    assert datetime.fromisoformat(old["heartbeat_at"]) < clock.now() - timedelta(hours=24)

    with pytest.raises(LeaseError, match="handover"):
        ops.pull(machine_b.context(monkeypatch), take=True)
    lease = json.loads(_remote_file(fake_github, "lease.json"))
    assert lease["holder"] == "machine-a"


def test_a_holder_whose_stale_lease_was_taken_drops_to_standby(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    token_ref, _token = _seed_runway(machine_a.settings())
    ops.push(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    clock.advance(hours=25)  # A went silent (offline), B takes over
    ops.pull(machine_b.context(monkeypatch), take=True, i_know_the_other_machine_is_off=True)

    # A comes back. Its own clone still says A holds the lease...
    local = read_lease(machine_a.instance_dir)
    assert local is not None and local.holder == "machine-a"
    # ...but a remote read records the takeover in the lease-check cache, and
    # the gated path trusts that fresh record without fetching again.
    remote_lease = ops.read_remote_lease(machine_a.settings())
    assert remote_lease is not None and remote_lease.holder == "machine-b"
    settings_a = machine_a.settings()
    assert not instance_is_active(settings_a)
    with pytest.raises(StandbyError, match="private repository says machine-b"):
        FileTokenStore.from_settings(settings_a).store(token_ref, TokenSet(access_token="x"))


def test_pull_verifies_before_taking_the_lease(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.handover(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    manifest = json.loads(_remote_file(fake_github, "manifest.json"))
    asset = (
        fake_github.release_dir(PRIVATE_REPO, manifest["release"]["tag"]) / "runway-database.db.gz"
    )
    asset.write_bytes(b"corrupted in transit")

    with pytest.raises(InstanceError, match="does not match manifest"):
        ops.pull(machine_b.context(monkeypatch))

    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] is None
    assert not instance_is_active(machine_b.settings())
    assert not (machine_b.root / "data" / "runway.db").exists()


def test_push_arbitrates_through_git_before_uploading(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    before = (machine.instance_dir / "lease.json").read_bytes()
    fake_github.reject_pushes = True  # another machine pushed first

    with pytest.raises(InstanceError, match="changed while this command ran"):
        ops.push(machine.context(monkeypatch))

    assert fake_github.uploads() == []
    assert not (machine.instance_dir / "manifest.json").exists()
    assert (machine.instance_dir / "lease.json").read_bytes() == before
    status = fake_github._real_git(
        ["git", "-C", str(machine.instance_dir), "status", "--porcelain"]
    )
    # Only the token store written after init remains, kept on disk and uncommitted.
    assert status.stdout.strip() == "?? tokens/"
    assert (machine.instance_dir / "tokens" / "platform-tokens.json").is_file()


def test_push_uses_dated_releases_updates_latest_and_keeps_the_newest_n(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    tags = []
    for _ in range(4):
        clock.advance(minutes=1)
        result = ops.push(machine.context(monkeypatch, instance_snapshot_keep=2))
        tags.append(result["tag"])
        assert ops._DATED_TAG_PATTERN.fullmatch(result["tag"])

    assert fake_github.releases(PRIVATE_REPO) == sorted([*tags[-2:], "snapshot-latest"])
    manifest = json.loads(_remote_file(fake_github, "manifest.json"))
    assert manifest["release"]["tag"] == tags[-1]
    latest = fake_github.release_dir(PRIVATE_REPO, "snapshot-latest")
    newest = fake_github.release_dir(PRIVATE_REPO, tags[-1])
    for name in ops.ASSET_NAMES:
        assert (latest / name).read_bytes() == (newest / name).read_bytes()


def test_pull_by_the_holder_refuses_to_overwrite_newer_local_work(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    ops.push(machine.context(monkeypatch))
    database = machine.settings().database_path
    later = datetime.now(UTC).timestamp() + 5
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE local_work (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    os.utime(database, (later, later))
    newer = database.read_bytes()

    with pytest.raises(InstanceError, match="--force"):
        ops.pull(machine.context(monkeypatch))
    assert database.read_bytes() == newer

    pulled = ops.pull(machine.context(monkeypatch), force=True)
    backup = Path(pulled["replaced_files_backup"])
    assert (backup / "runway.db").read_bytes() == newer
    assert database.read_bytes() != newer


def test_pull_by_the_holder_refuses_when_a_pending_wal_is_newer_than_the_snapshot(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    ops.push(machine.context(monkeypatch))
    settings = machine.settings()
    database = settings.database_path
    manifest = json.loads((machine.instance_dir / "manifest.json").read_text())
    wal = database.parent / f"{database.name}-wal"

    assert not ops._local_database_is_newer(settings, manifest)

    # A crash can leave committed changes sitting in the write-ahead log while
    # the main database file keeps its older modification time.
    wal.write_bytes(b"committed wal frames not yet checkpointed")
    later = datetime.now(UTC).timestamp() + 5
    os.utime(wal, (later, later))
    assert ops._local_database_is_newer(settings, manifest)

    with pytest.raises(InstanceError, match="--force"):
        ops.pull(machine.context(monkeypatch))

    # An empty or pre-snapshot WAL is not newer work.
    wal.write_bytes(b"")
    assert not ops._local_database_is_newer(settings, manifest)


def test_take_refuses_a_lease_whose_heartbeat_is_recent_on_the_remotes_own_clock(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.push(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)

    # 25 hours on B's clock make A's heartbeat look stale, but the remote lease
    # commit carries it as fresh: the age gap is clock skew, not a dead holder.
    clock.advance(hours=25)
    with pytest.raises(LeaseError, match="--i-know-the-other-machine-is-off") as refused:
        ops.pull(machine_b.context(monkeypatch), take=True)
    assert "machine-a" in str(refused.value)
    lease = json.loads(_remote_file(fake_github, "lease.json"))
    assert lease["holder"] == "machine-a"

    pulled = ops.pull(
        machine_b.context(monkeypatch),
        take=True,
        i_know_the_other_machine_is_off=True,
    )
    assert pulled["status"] == "pulled"
    assert instance_is_active(machine_b.settings())


def test_secrets_plaintext_never_touches_the_shared_temp_directory(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)

    [source] = fake_github.sops_inputs
    assert source.parent.parent == machine.instance_dir / "tmp"
    # Inside the instance directory, not directly in the shared temp directory.
    assert source.resolve().parent.parent.parent == machine.instance_dir.resolve()
    assert source.resolve().parent.parent != Path(tempfile.gettempdir()).resolve()
    assert not source.exists() and not source.parent.exists()
    if sys.platform == "win32":
        [acl] = [call for call in fake_github.calls if call[0] == "icacls"]
        assert "/inheritance:r" in acl
    ignored = fake_github._real_git(
        ["git", "-C", str(machine.instance_dir), "check-ignore", "-q", "tmp/x"]
    )
    assert ignored.returncode == 0


def test_every_external_command_has_a_timeout_and_no_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    # The parent shell may already set it; the runner must set it on its own.
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    # Git config injected through the environment must never reach a child git:
    # remote.<url>.pushurl and url.<base>.pushInsteadOf could redirect the push.
    monkeypatch.setenv("GIT_CONFIG", "/tmp/injected-config")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.x.pushurl")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "file:///tmp/injected")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'url.x.insteadof'='y'")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/user/.gitconfig")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        if args[0].endswith("slow"):
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])
        return _completed(args)

    monkeypatch.setattr(ops.shutil, "which", lambda name: name)
    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    # The git rows use the argument shape remote_git really builds: the four
    # GIT_CREDENTIALS items and -C before the subcommand.
    for args in (
        ["git", "status"],
        ["git", *ops.GIT_CREDENTIALS, "-C", "d", "push", "url", "HEAD:main"],
        ["git", *ops.GIT_CREDENTIALS, "-C", "d", "fetch", "url", "+a:b"],
        ["git", *ops.GIT_CREDENTIALS, "-C", "d", "remote", "get-url", "origin"],
        ["gh", "repo", "view", "o/n"],
        ["gh", "release", "upload", "t", "f"],
        ["sops", "--encrypt"],
    ):
        ops.subprocess_runner(args)
    assert [call["timeout"] for call in seen] == [
        ops.COMMAND_TIMEOUT_SECONDS,
        ops.NETWORK_TIMEOUT_SECONDS,
        ops.NETWORK_TIMEOUT_SECONDS,
        ops.COMMAND_TIMEOUT_SECONDS,
        ops.COMMAND_TIMEOUT_SECONDS,
        ops.TRANSFER_TIMEOUT_SECONDS,
        instance_config.SOPS_TIMEOUT_SECONDS,
    ]
    assert all(call["env"]["GIT_TERMINAL_PROMPT"] == "0" for call in seen)
    assert all(call["env"]["GIT_CONFIG_NOSYSTEM"] == "1" for call in seen)
    for call in seen:
        env = call["env"]
        assert not any(
            name == "GIT_CONFIG"
            or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            or name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
            for name in env
        ), env
        # The user's own files still apply: the credential helper lives there.
        assert env["GIT_CONFIG_GLOBAL"] == "/home/user/.gitconfig"
        assert env["GIT_SSH_COMMAND"] == "ssh -i key"
    assert all(call["shell"] is False for call in seen)
    with pytest.raises(InstanceError, match="did not finish"):
        ops.subprocess_runner(["slow"])


def test_the_heartbeat_thread_beats_and_stops(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    clock.advance(hours=6)
    beater = ops.LeaseHeartbeat(
        machine.settings(),
        interval_seconds=3600,
        context_factory=lambda settings: InstanceContext(
            settings=settings, runner=fake_github, now=clock.now
        ),
    )
    beater.start()
    beater.stop()
    assert beater.last_result is not None and beater.last_result["status"] == "renewed"


# --- the instance operation lock ---------------------------------------------------


def _operation_lock_path(machine: Machine) -> Path:
    return machine.instance_dir / "tmp" / "instance.lock"


def test_concurrent_remote_lease_reads_all_see_the_holder(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    settings = machine.settings()

    def delayed_fetch(
        args: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = fake_github(args, cwd=cwd)
        if "fetch" in args:
            # Stand in for network latency: without serialized fetches into a
            # shared ref this is the window where a concurrent read sees nothing.
            time.sleep(0.2)
        return result

    holders: list[str | None] = []

    def reader() -> None:
        lease = ops.read_remote_lease(settings, delayed_fetch)
        holders.append(lease.holder if lease is not None else None)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert holders == ["machine-a"] * 6


def test_an_instance_command_waits_for_the_operation_lock_then_fails_clearly(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    monkeypatch.setattr(ops, "INSTANCE_LOCK_WAIT_SECONDS", 0.2)
    uploads_before = len(fake_github.uploads())

    with exclusive_file_lock(_operation_lock_path(machine), timeout=0):
        with pytest.raises(InstanceError, match="another runway instance operation"):
            ops.push(machine.context(monkeypatch))
        with pytest.raises(InstanceError, match="another runway instance operation"):
            ops.heartbeat(machine.context(monkeypatch))

    assert len(fake_github.uploads()) == uploads_before
    lease = read_lease(machine.instance_dir)
    assert lease is not None and lease.holder == "machine-a"


def test_the_heartbeat_thread_skips_a_beat_while_the_lock_is_held(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    clock.advance(hours=6)
    beater = ops.LeaseHeartbeat(
        machine.settings(),
        interval_seconds=3600,
        context_factory=lambda settings: InstanceContext(
            settings=settings, runner=fake_github, now=clock.now
        ),
    )
    with exclusive_file_lock(_operation_lock_path(machine), timeout=0):
        beater.beat()
        assert beater.last_result is not None and beater.last_result["status"] == "skipped"

    beater.beat()
    assert beater.last_result is not None and beater.last_result["status"] == "renewed"


# --- pull refuses while Runway is running; a failed install rolls back ----------


def _two_machines_with_a_released_lease(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> tuple[Machine, Machine]:
    machine_a = _machine(tmp_path, "machine-a", fake_github, clock)
    machine_b = _machine(tmp_path, "machine-b", fake_github, clock)
    ops.init(machine_a.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine_a.settings())
    ops.handover(machine_a.context(monkeypatch))
    ops.init(machine_b.context(monkeypatch), PRIVATE_REPO)
    return machine_a, machine_b


@pytest.mark.parametrize("lock_path", [api_lock_path, worker_lock_path], ids=["api", "worker"])
def test_pull_refuses_while_runway_holds_the_worker_lock(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    lock_path: Callable[[Settings], Path],
) -> None:
    _machine_a, machine_b = _two_machines_with_a_released_lease(
        tmp_path, fake_github, clock, monkeypatch
    )
    remote_head = _remote_head(fake_github, PRIVATE_REPO)

    # The API holds api.lock and the worker worker.lock for their lifetimes;
    # here the test itself stands in for either.
    with (
        exclusive_file_lock(lock_path(machine_b.settings()), timeout=0),
        pytest.raises(InstanceError, match="stop Runway"),
    ):
        ops.pull(machine_b.context(monkeypatch))

    # Refused before any lease commit or download: the remote is untouched.
    assert _remote_head(fake_github, PRIVATE_REPO) == remote_head
    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] is None
    assert not any(call[:3] == ["gh", "release", "download"] for call in fake_github.calls)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="only Windows refuses to rename an open SQLite file",
)
def test_pull_refuses_while_the_local_database_is_open(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine_a, machine_b = _two_machines_with_a_released_lease(
        tmp_path, fake_github, clock, monkeypatch
    )
    remote_head = _remote_head(fake_github, PRIVATE_REPO)
    database = machine_b.root / "data" / "runway.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    live = sqlite3.connect(database)
    live.execute("create table standby_state(x)")
    live.commit()  # B's Runway is running against this database
    try:
        with pytest.raises(InstanceError, match="stop Runway"):
            ops.pull(machine_b.context(monkeypatch))
    finally:
        live.close()

    assert _remote_head(fake_github, PRIVATE_REPO) == remote_head
    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] is None
    probe = sqlite3.connect(database)
    try:
        assert probe.execute("select name from sqlite_master").fetchall() == [("standby_state",)]
    finally:
        probe.close()


def test_a_failed_install_releases_the_lease_and_restores_the_database(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine_a, machine_b = _two_machines_with_a_released_lease(
        tmp_path, fake_github, clock, monkeypatch
    )
    database = machine_b.root / "data" / "runway.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"b standby database")

    def fail_mid_install(data_dir: Path, staging: Path, stamp: str) -> Path | None:
        # The same partial state _install_restored leaves when an os.replace
        # fails after the live database was already moved to the backup.
        backup = data_dir / "backups" / f"pre-instance-pull-{stamp}"
        backup.mkdir(parents=True)
        os.replace(data_dir / "runway.db", backup / "runway.db")
        raise OSError("simulated install failure")

    monkeypatch.setattr(ops, "_install_restored", fail_mid_install)

    with pytest.raises(InstanceError, match="could not be installed"):
        ops.pull(machine_b.context(monkeypatch))

    # The lease take was rolled back, locally and on the remote.
    assert json.loads(_remote_file(fake_github, "lease.json"))["holder"] is None
    local = read_lease(machine_b.instance_dir)
    assert local is not None and local.holder is None
    # And the previous local database is back in place.
    assert database.read_bytes() == b"b standby database"


# --- the transfer marker keeps the worker off during push/handover ---------------


def test_a_job_starting_during_the_snapshot_is_refused(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    settings = machine.settings()
    marker = instance_lease.instance_transfer_marker(settings)
    seen: dict[str, object] = {}
    real_create_release = ops.create_release

    def create_release_during_transfer(**kwargs):  # type: ignore[no-untyped-def]
        # After the idle wait passed, while the snapshot is being built, new
        # work must already be refused: it would land after the snapshot.
        seen["marker"] = marker.is_file()
        seen["active"] = instance_is_active(settings)
        try:
            instance_lease.ensure_active_or_raise(settings)
        except StandbyError as exc:
            seen["refusal"] = str(exc)
        return real_create_release(**kwargs)

    monkeypatch.setattr(ops, "create_release", create_release_during_transfer)

    pushed = ops.push(machine.context(monkeypatch))

    assert pushed["status"] == "pushed"
    assert seen == {
        "marker": True,
        "active": False,
        "refusal": seen["refusal"],
    }
    assert "transfer in progress" in str(seen["refusal"])
    # The marker is gone once the push finishes, and the machine is active.
    assert not marker.is_file()
    assert instance_is_active(settings)


def test_handover_keeps_the_transfer_marker_until_the_lease_is_released(
    tmp_path: Path, fake_github: FakeGitHub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    settings = machine.settings()
    marker = instance_lease.instance_transfer_marker(settings)
    seen: dict[str, object] = {}
    real_publish_lease = ops._publish_lease

    def publish_during_handover(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen["marker_on_release"] = marker.is_file()
        return real_publish_lease(*args, **kwargs)

    monkeypatch.setattr(ops, "_publish_lease", publish_during_handover)

    handed = ops.handover(machine.context(monkeypatch))

    assert handed["mode"] == "standby"
    # The only _publish_lease in a handover is the lease release at the end.
    assert seen["marker_on_release"] is True
    assert not marker.is_file()
    assert not instance_is_active(settings)


def test_a_running_api_holds_its_own_lock_and_never_the_workers(tmp_path: Path) -> None:
    # `runway worker` needs worker.lock to start; the API must not take it.
    from fastapi.testclient import TestClient

    from runway.api.app import create_app
    from runway.db import initialize_database

    settings = Settings(data_dir=tmp_path / "data")
    initialize_database(settings).engine.dispose()
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            pass
        with (
            pytest.raises(FileLockTimeout),
            exclusive_file_lock(api_lock_path(settings), timeout=0),
        ):
            pass


def test_the_instance_gitignore_keeps_every_lock_file_out_of_the_private_repository() -> None:
    # The token store keeps per-account refresh locks next to the tokens.
    lines = set(ops._GITIGNORE.splitlines())
    assert {"tokens/token.lock", "tokens/refresh-locks/", "tmp/"} <= lines
