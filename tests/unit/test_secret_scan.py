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


def _git(repo: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
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
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        input=input,
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


def _bash() -> str | None:
    found = shutil.which("bash")
    # On Windows, System32\bash.exe is WSL, not Git Bash; CI runs this on Linux.
    if found is None or "system32" in found.casefold():
        return None
    return found


BASH = _bash()
ZERO_SHA = "0" * 40


@pytest.mark.skipif(BASH is None, reason="bash (Git Bash on Windows) is not installed")
def test_ci_scans_every_pushed_commit_and_the_whole_history_of_a_new_branch(
    tmp_path: Path,
) -> None:
    assert BASH is not None
    repo = tmp_path / "pushed"
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
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    removal_parent = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
    # The scanner and the CI script stay untracked, as a checkout of the code would have them.
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "secret_scan.py")
    shutil.copy2(REPO_ROOT / "scripts" / "secret_scan_ci.sh", repo / "scripts")

    def ci(**event: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "EVENT_NAME": "push",
            "BEFORE_SHA": "",
            "HEAD_SHA": head,
            "PR_BASE_SHA": "",
            "PR_HEAD_SHA": "",
            **event,
        }
        return subprocess.run(
            [BASH, "scripts/secret_scan_ci.sh"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    # A push whose final tree is clean still fails on the commit in the middle.
    pushed = ci(BEFORE_SHA=base)
    assert pushed.returncode == 1, pushed.stdout + pushed.stderr
    assert "google-oauth-access-token" in pushed.stderr
    # A new branch (no upstream: before is all zeros) scans the whole history.
    new_branch = ci(BEFORE_SHA=ZERO_SHA)
    assert new_branch.returncode == 1 and "every commit" in new_branch.stdout
    # A before-SHA that no longer exists (force push) falls back to the whole history.
    gone = ci(BEFORE_SHA="1" * 40)
    assert gone.returncode == 1 and "every commit" in gone.stdout
    # A pull request scans its range.
    pull_request = ci(EVENT_NAME="pull_request", PR_BASE_SHA=base, PR_HEAD_SHA=head)
    assert pull_request.returncode == 1 and "google-oauth-access-token" in pull_request.stderr
    # Only the pushed range is scanned: a push of just the clean-up commit passes.
    clean = ci(BEFORE_SHA=removal_parent)
    assert clean.returncode == 0, clean.stdout + clean.stderr


def test_secret_scan_workflow_scans_all_history_and_cannot_be_cancelled() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "secret-scan.yml").read_text(encoding="utf-8")
    )
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 parses ``on:`` as True
    assert set(triggers) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    # No concurrency cancellation: a cancelled run could leave a pushed secret unscanned.
    assert "concurrency" not in workflow
    [job] = workflow["jobs"].values()
    steps = job["steps"]
    checkout = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    # Same pinned SHA as ci.yml, full history, no persisted credentials.
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    setup_python = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup_python["uses"] == "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
    assert any(
        "scripts/secret_scan.py --all-history" in str(step.get("run", "")) for step in steps
    ), "the repository is small enough to scan every commit every time"


def test_ci_workflow_no_longer_runs_the_secret_scan() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "secret_scan" not in ci


# --- p6 review fixes ----------------------------------------------------------

FAKE_AGE_KEY = "AGE-SECRET-KEY-" + "1" + "A" * 58
FAKE_OPENAI = "sk-" + "aB3xK9" * 5
FAKE_OPENAI_PROJ = "sk-" + "proj-" + "x9K2mN7pQ" * 3
FAKE_OPENAI_SVCACCT = "sk-" + "svcacct-" + "r4T8yU1i" * 3
FAKE_OPENAI_ADMIN = "sk-" + "admin-" + "w2E5rT8y" * 3
FAKE_GITHUB_TOKEN = "gh" + "p_" + "a1B2c3" * 6
FAKE_GITHUB_OAUTH = "gh" + "o_" + "Z9x8Y7" * 6
FAKE_ASSIGNED = "Rt5" + "yU8iO1pA4sD7fG9h"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"identity: {FAKE_AGE_KEY}", "age-secret-key"),
        (f'OPENAI_API_KEY="{FAKE_OPENAI}"', "openai-api-key"),
        (f"key = {FAKE_OPENAI_PROJ}", "openai-api-key"),
        (f"token {FAKE_OPENAI_SVCACCT}", "openai-api-key"),
        (f"{FAKE_OPENAI_ADMIN}", "openai-api-key"),
        (f"OPENAI_API_KEY={FAKE_ASSIGNED}", "api-key-assignment"),
        (f"SEARCH_API_KEY={FAKE_ASSIGNED}", "api-key-assignment"),
        (f"TWELVEDATA_API_KEY={FAKE_ASSIGNED}", "api-key-assignment"),
        (f'RUNWAY_SEARCH_API_KEY: "{FAKE_ASSIGNED}"', "api-key-assignment"),
        (f"token = {FAKE_GITHUB_TOKEN}", "github-token"),
        (f"oauth = {FAKE_GITHUB_OAUTH}", "github-token"),
        (f"Bearer {FAKE_GOOGLE}", "google-oauth-access-token"),
    ],
)
def test_review_token_shapes_and_assignments_are_caught(text: str, rule: str) -> None:
    rules = {finding.rule for finding in secret_scan.scan_text("config.env", text + "\n")}
    assert rule in rules, (text, rules)


@pytest.mark.parametrize(
    "text",
    [
        "from sk-" + "learn import svm",  # an ordinary ``sk-`` word is not a key
        "sk-" + "a" * 19,  # below the 20-character key body
        "docs mention sk-proj- and sk-svcacct- prefixes",
        "OPENAI_API_KEY=",
        "OPENAI_API_KEY=changeme",
        "OPENAI_API_KEY=<your key here>",
        "OPENAI_API_KEY=your-key-here",
        "MY_API_KEY=placeholder",
        "api_key=os.environ['OPENAI_API_KEY']",
        "openai_api_key: str | None = None",
        "your OPENAI_API_KEY environment variable",
        "AGE-SECRET-KEY-" + "1" + "A" * 57,  # one character short
        "gh" + "p_" + "a" * 35,  # 35 characters, below the 36-character body
        "the age identity file is sops/age/keys.txt",
    ],
)
def test_review_rules_do_not_flag_short_keys_placeholders_or_prose(text: str) -> None:
    assert secret_scan.scan_text("notes.md", text) == []


@pytest.mark.parametrize(
    "value",
    [
        "real" + "test" + "Value9xK2mQ",  # contains the word ``test`` but is not a placeholder
        "ab" + "xxx" + "1234567890abcdef",  # ``xxx`` inside a real-looking value
        "prodkey" + "your" + "123456789",  # ``your`` inside a real-looking value
    ],
)
def test_a_placeholder_word_inside_a_real_value_does_not_exempt_it(value: str) -> None:
    findings = secret_scan.scan_text("config.env", f"RUNWAY_TOKEN_KEY={value}\n")
    assert [finding.rule for finding in findings] == ["token-key-assignment"]


@pytest.mark.parametrize(
    "path",
    [
        "sops/age/keys.txt",
        "config/sops/age/keys.txt",
        "backups/master.agekey",
    ],
)
def test_age_key_material_is_refused(path: str) -> None:
    assert secret_scan.blocked_name(path) is not None


@pytest.mark.parametrize(
    "path",
    ["sops/age/key.txt", "sops/keys.txt", "age/keys.txt", "keys.txt", "notes.txt"],
)
def test_other_file_names_still_pass(path: str) -> None:
    assert secret_scan.blocked_name(path) is None


def _git_object(repo: Path, data: bytes) -> str:
    """Write DATA as a blob object in REPO and return its object id."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        env=env,
        input=data,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("ascii").strip()


def test_a_staged_typechange_is_scanned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    # a.txt becomes a symlink (status T) whose blob still holds the token text.
    sha = _git_object(repo, f'TOKEN = "{FAKE_GOOGLE}"\n'.encode())
    _git(repo, "update-index", "--cacheinfo", f"120000,{sha},a.txt")

    staged = _run(repo, "--staged")
    assert staged.returncode == 1, staged.stderr
    assert "google-oauth-access-token" in staged.stderr

    _git(repo, "commit", "--quiet", "-m", "typechange")
    history = _run(repo, "--all-history")
    assert history.returncode == 1 and "google-oauth-access-token" in history.stderr


def test_a_gitlink_commit_does_not_desynchronise_the_blob_reader(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # A submodule pointer (mode 160000) at a commit that exists in this object store.
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},aaa-sub")
    _git(repo, "commit", "--quiet", "-m", "gitlink")
    (repo / "zzz.py").write_text(f'TOKEN = "{FAKE_GOOGLE}"\n', encoding="utf-8")
    _git(repo, "add", "zzz.py")
    _git(repo, "commit", "--quiet", "-m", "after the gitlink")

    history = _run(repo, "--all-history")
    assert history.returncode == 1, history.stderr
    assert "zzz.py:1: google-oauth-access-token" in history.stderr


def test_file_names_with_newlines_are_scanned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # update-index refuses control characters, so the commit is built with mktree.
    a_sha = _git(repo, "rev-parse", "HEAD:a.txt").stdout.strip()
    token_sha = _git_object(repo, f'TOKEN = "{FAKE_GOOGLE}"\n'.encode())
    tree_input = f"100644 blob {a_sha}\ta.txt\x00100644 blob {token_sha}\twe\nird.txt\x00"
    tree = _git(repo, "mktree", "-z", input=tree_input).stdout.strip()
    leak = _git(repo, "commit-tree", tree, "-p", base, "-m", "newline name").stdout.strip()
    _git(repo, "update-ref", "HEAD", leak)

    in_range = _run(repo, "--range", f"{base}..HEAD")
    assert in_range.returncode == 1, in_range.stderr
    assert "google-oauth-access-token" in in_range.stderr
    history = _run(repo, "--all-history")
    assert history.returncode == 1 and "google-oauth-access-token" in history.stderr


def test_content_beyond_the_old_five_mebibyte_limit_is_scanned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    payload = b"a" * (5 * 1024 * 1024 + 100) + f'\nTOKEN = "{FAKE_GOOGLE}"\n'.encode()
    (repo / "big.txt").write_bytes(payload)
    _git(repo, "add", "big.txt")

    staged = _run(repo, "--staged")
    assert staged.returncode == 1, staged.stderr
    assert "google-oauth-access-token" in staged.stderr

    _git(repo, "commit", "--quiet", "-m", "big")
    history = _run(repo, "--all-history")
    assert history.returncode == 1 and "google-oauth-access-token" in history.stderr


def test_binary_database_and_archive_content_is_flagged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    # Innocent names: only the magic bytes give the content away.
    (repo / "data.txt").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    (repo / "pack.bin").write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 64)
    _git(repo, "add", "-f", "data.txt", "pack.bin")

    staged = _run(repo, "--staged")
    assert staged.returncode == 1, staged.stderr
    assert "SQLite" in staged.stderr and "gzip" in staged.stderr

    findings = secret_scan.scan_blob("data.txt", b"SQLite format 3\x00" + b"\x00" * 8)
    assert [finding.rule for finding in findings] == ["SQLite database binary"]
    findings = secret_scan.scan_blob("pack.bin", b"\x1f\x8b\x08\x00rest")
    assert [finding.rule for finding in findings] == ["gzip archive binary"]


# --- re-review fixes ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "backups/media.zip",
        "runway-operational-media.zip",
        "data/runway.db-journal",
        "data/runway.db-wal",
        "data/runway.db-shm",
        "data/runway.sqlite",
        "data/runway.sqlite3",
    ],
)
def test_archive_and_database_sidecar_names_are_refused(path: str) -> None:
    assert secret_scan.blocked_name(path) is not None


def test_zip_binary_magic_is_flagged() -> None:
    findings = secret_scan.scan_blob("innocent.txt", b"PK\x03\x04" + b"\x00" * 32)
    assert [finding.rule for finding in findings] == ["binary archive in history"]

    repo_file = secret_scan.scan_blob("x.zip", b"PK\x03\x04" + b"\x00" * 8)
    rules = {finding.rule for finding in repo_file}
    assert "binary archive in history" in rules
    assert any("zip" in rule for rule in rules)


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_is_detected_below_full_nul_saturation(encoding: str) -> None:
    # About a third non-Latin characters: ~60% NULs on one side, not nearly all.
    line = f"token = '{FAKE_GOOGLE}' " + chr(0x4E2D) * 30
    data = line.encode(encoding)
    text = secret_scan.decode_text(data)
    assert text is not None
    assert [finding.rule for finding in secret_scan.scan_blob("notes.txt", data)] == [
        "google-oauth-access-token"
    ]


def test_text_with_stray_nul_bytes_is_scanned_after_stripping() -> None:
    data = b"line one\0\nTOKEN = '" + FAKE_GOOGLE.encode() + b"'\0\n"
    findings = secret_scan.scan_blob("notes.txt", data)
    assert [finding.rule for finding in findings] == ["google-oauth-access-token"]


def test_a_refresh_token_after_a_colon_is_caught() -> None:
    refresh = "1//" + "0gAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    findings = secret_scan.scan_text("config.env", f"refresh:{refresh}\n")
    assert "google-refresh-token" in {finding.rule for finding in findings}
