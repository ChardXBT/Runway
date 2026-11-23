"""Push-destination guards for ``runway instance`` (review p6).

The private companion repository must only ever receive data meant for the
verified GitHub repository. These tests let real ``git`` resolve the push the
way production does: a remote *named* by the verified URL,
``url.<base>.pushInsteadOf`` whose base contains a space, ``GIT_CONFIG_*``
environment injection and several push URLs must never redirect the push, and
the repository must be re-confirmed private right before the lease/manifest
commit and again before the snapshot upload.

``test_instance_commands.py`` provides the FakeGitHub harness (a local bare
repository stands in for each GitHub repository); several tests here instead
drive :func:`runway.operations.instance.subprocess_runner` directly, so the
isolated git environment is exercised end to end against real git.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_instance_commands import (
    PRIVATE_REPO,
    PUBLIC_REPO,
    Clock,
    FakeGitHub,
    _completed,
    _git_in,
    _machine,
    _remote_head,
    _seed_runway,
    fake_sops,
)

import runway.config.instance as instance_config
from runway.config import Settings
from runway.operations import instance as ops
from runway.operations.instance import InstanceContext, InstanceError

VERIFIED_URL = f"https://github.com/{PRIVATE_REPO}.git"


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _plain_git(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, shell=False
    )
    assert result.returncode == 0, result.stderr
    return result


def _bare(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain_git("init", "--quiet", "--bare", str(path))
    return path


def _has_main(bare: Path) -> bool:
    result = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _make_clone(root: Path, name: str, origin: str = VERIFIED_URL) -> Path:
    clone = root / name
    _plain_git("init", "--quiet", str(clone))
    (clone / "lease.json").write_text("{}\n", encoding="utf-8")
    _git(clone, "add", ".")
    _git(clone, "commit", "--quiet", "-m", "x")
    _git(clone, "remote", "add", "origin", origin)
    return clone


@pytest.fixture()
def real_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean git environment: private global config, no network transports."""
    gitconfig = tmp_path / "global.gitconfig"
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = main\n[user]\n\temail = t@example.invalid\n\tname = t\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    # A hostile shell environment must never reach the git child processes.
    for name in ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)
    for name in [k for k in os.environ if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _real_context(root: Path) -> InstanceContext:
    """An InstanceContext running the real subprocess_runner (isolated env)."""
    return InstanceContext(settings=Settings(_env_file=None, data_dir=root / "data"))


@pytest.fixture()
def fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """The same fake GitHub as test_instance_commands (fixtures are not shared)."""
    (tmp_path / "github").mkdir()
    fake = FakeGitHub(tmp_path / "github")
    fake.add_repo(PRIVATE_REPO)
    fake.add_repo(PUBLIC_REPO, visibility="PUBLIC")
    monkeypatch.setattr(instance_config, "sops_runner", lambda args: fake_sops(list(args)))
    monkeypatch.setattr(ops, "default_runner", fake)
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "profile" / "machine-id"))
    for name in ("RUNWAY_INSTANCE_DIR", "RUNWAY_INSTANCE_REPOSITORY", "RUNWAY_MACHINE_ID"):
        monkeypatch.delenv(name, raising=False)
    yield fake


@pytest.fixture()
def clock() -> Clock:
    return Clock()


# --- remotes named like URLs ---------------------------------------------------


@pytest.mark.parametrize("key", ["url", "pushurl"])
def test_a_remote_named_by_the_verified_url_never_redirects_the_push(
    real_git: Path, key: str
) -> None:
    foreign = _bare(real_git / "remotes", "foreign.git")
    clone = _make_clone(real_git, "clone")
    # A remote literally named https://github.com/<owner>/<name>.git is a
    # configured remote whose name equals the push argument: git resolves the
    # "URL" to this remote and pushes wherever it points.
    _git(clone, "config", f"remote.{VERIFIED_URL}.{key}", str(foreign))
    ctx = _real_context(real_git)

    with pytest.raises(InstanceError, match="remote named"):
        ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        ops._push_head(ctx, clone, PRIVATE_REPO)

    assert not _has_main(foreign)


def test_two_pushurls_on_a_url_named_remote_never_reach_the_foreign_one(
    real_git: Path,
) -> None:
    verified_stand_in = _bare(real_git / "remotes", "verified-stand-in.git")
    foreign = _bare(real_git / "remotes", "foreign.git")
    clone = _make_clone(real_git, "clone")
    # git pushes to every pushurl of the resolved remote; one of them is foreign.
    _git(clone, "config", f"remote.{VERIFIED_URL}.pushurl", str(verified_stand_in))
    _git(clone, "config", "--add", f"remote.{VERIFIED_URL}.pushurl", str(foreign))
    ctx = _real_context(real_git)

    with pytest.raises(InstanceError, match="remote named"):
        ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        ops._push_head(ctx, clone, PRIVATE_REPO)

    assert not _has_main(foreign)


# --- url.<base>.insteadOf / pushInsteadOf --------------------------------------


@pytest.mark.parametrize("rule", ["insteadOf", "pushInsteadOf"])
def test_a_rewrite_base_containing_a_space_is_parsed_and_refused(real_git: Path, rule: str) -> None:
    # The base contains a space, so a naive line.split() can never see it.
    spaced = real_git / "sp ace" / "github.com"
    foreign = _bare(spaced, "owner/runway-instance.git")
    clone = _make_clone(real_git, "clone")
    _git(clone, "config", f"url.{spaced.as_posix()}/.{rule}", "https://github.com/")
    ctx = _real_context(real_git)

    # With an HTTPS origin the rewrite also shows up in `remote get-url`; either
    # refusal keeps the foreign repository empty.
    with pytest.raises(InstanceError, match="pushes to|insteadOf"):
        ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        ops._push_head(ctx, clone, PRIVATE_REPO)

    assert not _has_main(foreign)


@pytest.mark.parametrize("rule", ["insteadOf", "pushInsteadOf"])
def test_a_rewrite_base_with_a_space_and_an_ssh_origin_is_refused(
    real_git: Path, rule: str
) -> None:
    # The -z insteadOf check matters most when origin is SSH: an HTTPS-only rule
    # cannot rewrite it, so get-url looks clean while the explicit HTTPS push
    # is rewritten.
    spaced = real_git / "another dir" / "github.com"
    foreign = _bare(spaced, "owner/runway-instance.git")
    clone = _make_clone(real_git, "clone", origin=f"git@github.com:{PRIVATE_REPO}.git")
    _git(clone, "config", f"url.{spaced.as_posix()}/.{rule}", "https://github.com/")
    ctx = _real_context(real_git)

    with pytest.raises(InstanceError, match="insteadOf"):
        ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        ops._push_head(ctx, clone, PRIVATE_REPO)

    assert not _has_main(foreign)


# --- GIT_CONFIG_* environment injection -----------------------------------------


def test_git_config_count_environment_injection_is_never_seen(
    real_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _bare(real_git / "remotes", "injected.git")
    clone = _make_clone(real_git, "clone")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"remote.{VERIFIED_URL}.pushurl")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(foreign))
    # Control: an un-isolated git really does see the injected remote section.
    plain = subprocess.run(
        ["git", "-C", str(clone), "config", "--get-regexp", r"^remote\."],
        capture_output=True,
        text=True,
        env=os.environ,
        check=False,
        shell=False,
    )
    assert f"remote.{VERIFIED_URL}.pushurl".casefold() in plain.stdout.casefold()

    ctx = _real_context(real_git)
    # The runner strips GIT_CONFIG_*: the injected remote simply does not exist.
    ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        # https is unreachable here; what matters is the injected repo stays empty.
        ops._push_head(ctx, clone, PRIVATE_REPO)
    assert not _has_main(foreign)


def test_git_config_parameters_injection_is_never_seen(
    real_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _bare(real_git / "remotes", "injected.git")
    clone = _make_clone(real_git, "clone")
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        f"'remote.{VERIFIED_URL}.pushurl'='{foreign.as_posix()}'",
    )
    ctx = _real_context(real_git)
    ops.verify_push_destination(ctx, clone, PRIVATE_REPO)
    with pytest.raises(InstanceError):
        ops._push_head(ctx, clone, PRIVATE_REPO)
    assert not _has_main(foreign)


# --- the effective destination is confirmed by a dry-run ------------------------


def test_the_dry_run_destination_must_be_the_verified_repository(real_git: Path) -> None:
    """A verified ``To <url>`` line lets the real push run with identical args."""
    clone = _make_clone(real_git, "clone")
    calls: list[list[str]] = []

    def runner(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[0] == "git" and "push" in args and "--dry-run" in args:
            return _completed(
                args,
                stdout=f"To {VERIFIED_URL}\n*\tHEAD:refs/heads/main\t[new branch]\nDone\n",
            )
        # GIT_ALLOW_PROTOCOL=file keeps the real push from reaching the network.
        return ops.subprocess_runner(args, cwd=cwd)

    ctx = InstanceContext(
        settings=Settings(_env_file=None, data_dir=real_git / "data"), runner=runner
    )
    result = ops._push_head(ctx, clone, PRIVATE_REPO)
    assert any("push" in call and "--dry-run" not in call for call in calls)
    # It attempted the real push only after the verified dry-run; https is
    # unreachable here, so the push itself cannot succeed.
    assert result.returncode != 0


@pytest.mark.parametrize(
    "to_lines",
    [
        ["To /tmp/foreign-bare.git"],
        ["To https://github.com/owner/other-repo.git"],
        [f"To {VERIFIED_URL}", "To /tmp/second-pushurl.git"],
        [],
    ],
    ids=["local-path", "other-github-repo", "one-of-two-foreign", "no-to-line"],
)
def test_a_foreign_or_missing_dry_run_destination_refuses_the_push(
    real_git: Path, to_lines: list[str]
) -> None:
    clone = _make_clone(real_git, "clone")
    pushes: list[list[str]] = []

    def runner(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[0] == "git" and "push" in args and "--dry-run" in args:
            body = "".join(
                f"{line}\n=\tHEAD:refs/heads/main\t[up to date]\nDone\n" for line in to_lines
            )
            return _completed(args, stdout=body)
        if args[0] == "git" and "push" in args:
            pushes.append(list(args))
            return _completed(args)
        return ops.subprocess_runner(args, cwd=cwd)

    ctx = InstanceContext(
        settings=Settings(_env_file=None, data_dir=real_git / "data"), runner=runner
    )
    with pytest.raises(InstanceError, match="push"):
        ops._push_head(ctx, clone, PRIVATE_REPO)
    assert pushes == []  # nothing was pushed


# --- the whole push through the FakeGitHub harness ------------------------------


def test_a_clean_clone_pushes_after_a_verified_dry_run(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    (machine.instance_dir / "README.md").write_text("# v2\n", encoding="utf-8")
    _git_in(fake_github, machine.instance_dir, "commit", "--quiet", "-am", "more")
    head = _remote_head(fake_github, PRIVATE_REPO)
    ctx = machine.context(monkeypatch)

    pushed = ops._push_head(ctx, machine.instance_dir, PRIVATE_REPO)

    assert pushed.returncode == 0
    assert _remote_head(fake_github, PRIVATE_REPO) != head
    push_calls = [call for call in fake_github.calls if "push" in call and call[0] == "git"]
    dry_run, real_push = push_calls[-2], push_calls[-1]
    assert "--dry-run" in dry_run and "--porcelain" in dry_run
    assert "--dry-run" not in real_push
    # The real push uses the identical remote/refspec arguments as the dry-run.
    mode_flags = {"--dry-run", "--porcelain", "--quiet"}
    assert [a for a in dry_run if a not in mode_flags] == [
        a for a in real_push if a not in mode_flags
    ]


def test_env_injected_pushurl_is_ignored_and_the_push_lands_in_the_verified_repo(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    injected = fake_github.add_repo("owner/injected", visibility="PRIVATE")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"remote.{VERIFIED_URL}.pushurl")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(injected))

    pushed = ops.push(machine.context(monkeypatch))

    assert pushed["status"] == "pushed"
    assert not _has_main(injected)


def test_a_public_flip_before_the_lease_push_refuses(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    head = _remote_head(fake_github, PRIVATE_REPO)
    real_create_release = ops.create_release

    def flip_after_snapshot(**kwargs):  # type: ignore[no-untyped-def]
        result = real_create_release(**kwargs)
        fake_github.repos[PRIVATE_REPO.casefold()]["visibility"] = "PUBLIC"
        return result

    monkeypatch.setattr(ops, "create_release", flip_after_snapshot)

    with pytest.raises(InstanceError, match="public"):
        ops.push(machine.context(monkeypatch))

    assert fake_github.uploads() == []
    # The lease/manifest commit never left this clone either.
    assert _remote_head(fake_github, PRIVATE_REPO) == head


def test_a_public_flip_before_the_snapshot_upload_refuses(
    tmp_path: Path,
    fake_github: FakeGitHub,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _machine(tmp_path, "machine-a", fake_github, clock)
    ops.init(machine.context(monkeypatch), PRIVATE_REPO)
    _seed_runway(machine.settings())
    real_commit_and_push = ops._commit_and_push

    def flip_after_lease_push(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_commit_and_push(*args, **kwargs)
        fake_github.repos[PRIVATE_REPO.casefold()]["visibility"] = "PUBLIC"
        return result

    monkeypatch.setattr(ops, "_commit_and_push", flip_after_lease_push)

    with pytest.raises(InstanceError, match="public"):
        ops.push(machine.context(monkeypatch))

    # The lease/manifest commit landed, but not a single asset was uploaded.
    assert fake_github.uploads() == []
