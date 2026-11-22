"""The public-repository secret scan (spec 07 §6).

Fake tokens are assembled at run time so this file never contains one.
"""

from __future__ import annotations

import importlib.util
import os
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
FAKE_CLIENT_SECRET = "client_secret" + "=GOCSPX-fakefakefake"


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
        ("client_secret" + ' = "GOCSPX-fakefakefake"', "client-secret-assignment"),
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
