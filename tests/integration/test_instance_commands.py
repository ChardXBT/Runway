"""``runway instance`` end to end against a fake GitHub.

``gh`` is never executed: :class:`FakeGitHub` answers every ``gh`` call, keeps
release assets in a temporary directory and backs each "GitHub" repository
with a local bare repository. ``git`` is real, but every remote URL is rewritten
to that local bare repository and ``GIT_ALLOW_PROTOCOL=file`` makes any other
transport fail, so no test can reach the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
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
from runway.operations import instance as ops
from runway.operations.instance import InstanceContext, InstanceError
from runway.operations.instance_lease import (
    LeaseError,
    StandbyError,
    instance_is_active,
    read_lease,
)
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore

PRIVATE_REPO = "owner/runway-instance"
FAKE_CIPHER_PREFIX = "fake-sops-ciphertext:"


def _completed(
    args: Sequence[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), code, stdout=stdout, stderr=stderr)


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        self.git_env = {
            **os.environ,
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
        self.repos[name.casefold()] = {
            "name": name,
            "visibility": visibility,
            "bare": bare,
            "release": None,
        }
        return bare

    def uploads(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["gh", "release", "upload"]]

    def _real_git(
        self, args: Sequence[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=cwd,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[0] == "git":
            return self._real_git(args, cwd)
        if args[0] == "gh":
            return self._gh(args)
        if args[0] == "sops":
            return fake_sops(args)
        if args[0] == "age-keygen":
            return fake_age_keygen(args)
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
            url = f"https://github.com/{repo['name']}.git"
            result = self._real_git(["git", "clone", "--quiet", str(repo["bare"]), str(target)])
            assert result.returncode == 0, result.stderr
            for git_args in (
                ["remote", "set-url", "origin", url],
                ["config", f"url.{Path(repo['bare']).as_posix()}.insteadOf", url],
            ):
                configured = self._real_git(["git", "-C", str(target), *git_args])
                assert configured.returncode == 0, configured.stderr
            return _completed(args)
        repo = self._repo(_option(args, "--repo"))
        assert repo is not None, args
        release_dir = self.root / "releases" / repo["name"].replace("/", "__") / args[3]
        if command == ["release", "view"]:
            return _completed(args, 0 if release_dir.is_dir() else 1, stdout="{}")
        if command == ["release", "create"]:
            release_dir.mkdir(parents=True)
            return _completed(args)
        if command == ["release", "upload"]:
            assert release_dir.is_dir() and "--clobber" in args
            for value in args[4:]:
                if value.startswith("--"):
                    break
                shutil.copy2(value, release_dir / Path(value).name)
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
    release_dir = fake.root / "releases" / f"{owner}__{name}" / tag
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
    fake.add_repo("owner/public-showcase", visibility="PUBLIC")
    # Settings decrypt secrets.enc.env through the same fake sops.
    monkeypatch.setattr(instance_config, "sops_runner", lambda args: fake_sops(list(args)))
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
    store = TokenStore.from_settings(settings)
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
        ops.init(machine.context(monkeypatch), "owner/public-showcase")

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
    legacy = TokenStore(machine.root / "data" / "secrets")
    legacy.store("legacy-ref", TokenSet(access_token="legacy-access"))
    legacy_key = legacy.key_path.read_text().strip()
    fixture_secret = "GOCSPX-fixture-client-secret-value"

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
    token = TokenStore.from_settings(settings).load("legacy-ref")
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
    restored = TokenStore.from_settings(settings_b).load(token_ref)
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
    unguarded = TokenStore(
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
    for git_args in (["commit", "-q", "-am", "newer snapshot"], ["push", "-q"]):
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
    assert ops.pull(machine_b.context(monkeypatch), take=True)["status"] == "pulled"
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
    marker = machine.root / "data" / "runtime" / "worker.busy"
    marker.parent.mkdir(parents=True)
    marker.write_text("publishing", encoding="utf-8")

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
    marker.write_text("publishing", encoding="utf-8")
    context = machine.context(monkeypatch)
    context.sleep = lambda seconds: clock.advance(seconds=seconds)
    with pytest.raises(InstanceError, match="still publishing"):
        ops.push(context, wait_seconds=12)
    with pytest.raises(InstanceError, match="still publishing"):
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

    TokenStore.from_settings(machine.settings()).store(token_ref, token)
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

    result = CliRunner().invoke(app, ["instance", "init", "--repo", "owner/public-showcase"])

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
