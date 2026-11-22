"""The public-repository secret scan (spec 07 §6).

Fake tokens are assembled at run time so this file never contains one.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "secret_scan.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runway_secret_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


secret_scan = _load()

FAKE_GOOGLE = "ya29" + "." + "a0AfB_fakeFAKEfake0123456789abcdef"
FAKE_META = "EAAC" + "ZCfakeFAKE0123456789abcdefABCDEF"
FAKE_TIKTOK = "act" + "." + "fakeFAKE0123456789abcdefABCDEF!4567"
FAKE_RUNWAY = "rwy_" + "live_" + "fakeFAKE0123456789ab"
FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
FAKE_CLIENT_SECRET = "client_secret" + "=Zq8Rw2Lp5Tn7Vb3Xm9Kc"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"token = '{FAKE_GOOGLE}'", "google-oauth-access-token"),
        (f"Authorization: Bearer {FAKE_META}", "meta-access-token"),
        (f'{{"access_token": "{FAKE_TIKTOK}"}}', "tiktok-access-token"),
        (f"key: {FAKE_RUNWAY}", "runway-live-api-key"),
        (FAKE_PEM, "private-key-block"),
        (f"https://example.invalid/token?{FAKE_CLIENT_SECRET}&x=1", "client-secret-assignment"),
        ("client_secret" + ' = "Zq8Rw2Lp5Tn7Vb3Xm9Kc"', "client-secret-assignment"),
    ],
)
def test_token_shaped_strings_are_caught(text: str, rule: str) -> None:
    findings = secret_scan.scan_text("notes.md", f"line one\n{text}\n")
    assert [(finding.rule, finding.line) for finding in findings] == [(rule, 2)]


@pytest.mark.parametrize(
    "text",
    [
        "Grep for `ya29.`, `EAAC`, `act.` and `client_secret=` patterns.",
        "youtube_client_secret: str | None = None",
        'youtube_client_secret="yt-secret"',
        "RUNWAY_YOUTUBE_CLIENT_SECRET=",
        "client_secret={client_secret}",
        "react.createElement(contract.something_long_enough_here)",
        "BEGIN PRIVATE KEY headers are refused",
    ],
)
def test_documentation_and_settings_are_not_flagged(text: str) -> None:
    assert secret_scan.scan_text("docs/notes.md", text) == []


@pytest.mark.parametrize(
    "path",
    [
        "data/runway.db",
        "data/secrets/token.key",
        ".env",
        "apps/web/.env.local",
        "data/secrets/platform-tokens.json",
        "runway-instance/secrets.enc.env",
        "lease.json",
    ],
)
def test_private_file_names_are_refused(path: str) -> None:
    assert secret_scan.blocked_name(path) is not None


def test_env_example_is_allowed() -> None:
    assert secret_scan.blocked_name(".env.example") is None


def test_findings_never_print_the_secret(tmp_path: Path) -> None:
    leaked = tmp_path / "leak.py"
    leaked.write_text(f"TOKEN = '{FAKE_GOOGLE}'\n", encoding="utf-8")
    result = _run(tmp_path, str(leaked))
    assert result.returncode == 1
    assert "google-oauth-access-token" in result.stderr
    assert FAKE_GOOGLE not in result.stderr + result.stdout


def test_allowlist_matches_one_exact_finding(tmp_path: Path) -> None:
    text = f"{FAKE_CLIENT_SECRET}\n"
    [finding] = secret_scan.scan_text("fixture.py", text)
    allowed = {("fixture.py", finding.rule, finding.fingerprint)}
    sources = [("fixture.py", lambda: text.encode())]
    assert secret_scan.scan(sources, allowed) == []
    other = [("other.py", lambda: text.encode())]
    assert len(secret_scan.scan(other, allowed)) == 1


def test_staged_fake_token_file_and_token_string_fail_the_scan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    assert _run(repo, "--staged").returncode == 0

    (repo / "data").mkdir()
    (repo / "data" / "platform-tokens.json").write_bytes(b"gAAAAAfake-fernet-blob")
    (repo / "config.py").write_text(f'ACCESS = "{FAKE_GOOGLE}"\n', encoding="utf-8")
    _git(repo, "add", "-f", "data/platform-tokens.json", "config.py")

    result = _run(repo, "--staged")
    assert result.returncode == 1
    assert "data/platform-tokens.json: platform token store must not be committed" in result.stderr
    assert "config.py:1: google-oauth-access-token" in result.stderr

    # The index is what counts: fixing only the working tree does not pass.
    (repo / "config.py").write_text("ACCESS = None\n", encoding="utf-8")
    assert "config.py:1" in _run(repo, "--staged").stderr


def test_the_current_tracked_tree_passes() -> None:
    result = _run(REPO_ROOT, "--tracked")
    assert result.returncode == 0, result.stderr


# --- review fixes -------------------------------------------------------------------------

FAKE_GOCSPX = "GOCSPX" + "-Ab3dEf6hIj9kLm2nOp5qRs8tUv1w"
FAKE_FERNET = "q3mW7xK2pL9vB4nR8tY1uE6iO0aS5dF2gH7jK3lZ9xC="
FAKE_GOOGLE_REFRESH = "1//" + "0gAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
FAKE_TIKTOK_REFRESH = "rft" + "." + "AbCdEfGhIjKlMnOpQrStUvWx0123"
FAKE_IGAA = "IGAA" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
FAKE_IGQV = "IGQV" + "JAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
FAKE_TIKTOK_SECRET = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"RUNWAY_YOUTUBE_CLIENT_SECRET={FAKE_TIKTOK_SECRET}", "client-secret-assignment"),
        (f"youtube_client_secret={FAKE_TIKTOK_SECRET}", "client-secret-assignment"),
        (f'    "client_secret": "{FAKE_TIKTOK_SECRET}",', "client-secret-assignment"),
        (f"RUNWAY_TIKTOK_CLIENT_SECRET = '{FAKE_TIKTOK_SECRET}'", "client-secret-assignment"),
        (f"RUNWAY_INSTAGRAM_APP_SECRET={FAKE_TIKTOK_SECRET}", "app-secret-assignment"),
        (f"RUNWAY_TOKEN_KEY={FAKE_FERNET}", "token-key-assignment"),
        (f"RUNWAY_TOKEN_KEYS={FAKE_FERNET},{FAKE_FERNET}", "token-key-assignment"),
        (f"refresh = '{FAKE_GOOGLE_REFRESH}'", "google-refresh-token"),
        (f"refresh_token: {FAKE_TIKTOK_REFRESH}", "tiktok-refresh-token"),
        (f"token={FAKE_IGAA}", "instagram-access-token"),
        (f"token={FAKE_IGQV}", "instagram-access-token"),
    ],
)
def test_platform_credential_formats_are_caught(text: str, rule: str) -> None:
    findings = secret_scan.scan_text("config.env", text + "\n")
    assert rule in {finding.rule for finding in findings}, findings


def test_google_client_secrets_are_caught_anywhere() -> None:
    for text in (FAKE_GOCSPX, f'{{"installed": {{"client_secret": "{FAKE_GOCSPX}"}}}}'):
        rules = {finding.rule for finding in secret_scan.scan_text("client_secret_1.json", text)}
        assert "google-client-secret" in rules


@pytest.mark.parametrize(
    "text",
    [
        '"client_secret": settings.youtube_client_secret,',
        "client_secret=client_secret,",
        "client_secret=os.environ['RUNWAY_YOUTUBE_CLIENT_SECRET'],",
        "RUNWAY_YOUTUBE_CLIENT_SECRET=...",
        "RUNWAY_TOKEN_KEY={FAKE_KEY}",
        "token_key=Fernet.generate_key().decode('ascii'),",
        "add RUNWAY_TOKEN_KEY to the instance's secrets.enc.env",
        'tiktok_client_secret="tt-secret-abc"',
        "RUNWAY_YOUTUBE_CLIENT_SECRET=<your client secret>",
    ],
)
def test_code_references_and_placeholders_are_not_flagged(text: str) -> None:
    assert secret_scan.scan_text("src/module.py", text) == []


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_files_are_decoded_and_scanned(encoding: str) -> None:
    data = f"token = '{FAKE_GOOGLE}'\r\n".encode(encoding)
    findings = secret_scan.scan_blob("notes.txt", data)
    assert [finding.rule for finding in findings] == ["google-oauth-access-token"]


def test_real_binary_is_not_decoded() -> None:
    assert secret_scan.decode_text(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + FAKE_GOOGLE.encode()) is None


@pytest.mark.parametrize(
    "path",
    [
        "backups/runway-database.db.gz",
        "runway-operational-media.zip",
        "data/runway.db-wal",
        "data/runway.db-shm",
        "data/runway.sqlite",
        "data/runway.sqlite3",
    ],
)
def test_snapshot_files_are_refused(path: str) -> None:
    assert secret_scan.blocked_name(path) is not None


def test_snapshot_patterns_are_ignored_by_git() -> None:
    for path in (
        "x/runway-database.db.gz",
        "runway-operational-media.zip",
        "a.sqlite",
        "a.sqlite3",
        "a.db-wal",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, path


def test_a_secret_added_then_removed_is_caught_in_the_commit_range(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "config.py").write_text(f'ACCESS = "{FAKE_GOOGLE}"\n', encoding="utf-8")
    _git(repo, "add", "config.py")
    _git(repo, "commit", "--quiet", "-m", "leak")
    (repo / "config.py").write_text("ACCESS = None\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "remove the leak")

    assert _run(repo, "--tracked").returncode == 0  # the final tree is clean
    in_range = _run(repo, "--range", f"{base}..HEAD")
    assert in_range.returncode == 1
    assert "config.py:1: google-oauth-access-token" in in_range.stderr
    assert FAKE_GOOGLE not in in_range.stderr
    history = _run(repo, "--all-history")
    assert history.returncode == 1 and "google-oauth-access-token" in history.stderr
    assert _run(repo, "--range", f"{base}..{base}").returncode == 0


def test_the_whole_history_of_this_repository_passes() -> None:
    result = _run(REPO_ROOT, "--all-history")
    assert result.returncode == 0, result.stderr


def _hook_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "hooked"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "secret_scan.py")
    shutil.copy2(REPO_ROOT / ".githooks" / "pre-commit", repo / ".githooks" / "pre-commit")
    _git(repo, "init", "--quiet")
    return repo


POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else shutil.which("pwsh")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_install_hooks_sets_core_hooks_path(tmp_path: Path) -> None:
    assert POWERSHELL is not None
    repo = _hook_repo(tmp_path)
    command = [POWERSHELL, "-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        command += ["-ExecutionPolicy", "Bypass"]
    result = subprocess.run(
        [
            *command,
            "-File",
            str(REPO_ROOT / "scripts" / "install_hooks.ps1"),
            "-RepositoryRoot",
            str(repo),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"


def test_the_pre_commit_hook_refuses_a_fake_token_commit(tmp_path: Path) -> None:
    repo = _hook_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".githooks")
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def commit(message: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "commit", "--quiet", "-m", message],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "README.md", "scripts", ".githooks")
    clean = commit("clean")
    assert clean.returncode == 0, clean.stderr

    (repo / "platform-tokens.json").write_bytes(b"gAAAAAfake-fernet-blob")
    (repo / "leak.py").write_text(f"T = '{FAKE_GOOGLE}'\n", encoding="utf-8")
    _git(repo, "add", "-f", "platform-tokens.json", "leak.py")
    refused = commit("leak")
    assert refused.returncode != 0
    assert "platform token store" in refused.stderr
    assert "google-oauth-access-token" in refused.stderr
    assert FAKE_GOOGLE not in refused.stderr
