from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest

import runway.config.instance as instance_config
from runway.config import Settings
from runway.config.instance import InstanceConfigError, decrypt_dotenv, parse_dotenv

FAKE_KEY = "x" * 43 + "="


class FakeSops:
    """Stands in for the sops binary: returns fixed dotenv text for --decrypt."""

    def __init__(self, plaintext: str, *, returncode: int = 0, stderr: str = "") -> None:
        self.plaintext = plaintext
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            list(args), self.returncode, stdout=self.plaintext, stderr=self.stderr
        )


def _instance(tmp_path: Path, toml: str | None = None, secrets: bool = True) -> Path:
    instance_dir = tmp_path / "runway-instance"
    instance_dir.mkdir()
    if toml is not None:
        (instance_dir / "instance.toml").write_text(textwrap.dedent(toml), encoding="utf-8")
    if secrets:
        (instance_dir / "secrets.enc.env").write_text("ciphertext only\n", encoding="utf-8")
    return instance_dir


@pytest.fixture(autouse=True)
def no_instance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RUNWAY_INSTANCE_DIR", "RUNWAY_CHANNEL_NAME", "RUNWAY_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)


def test_unset_instance_dir_keeps_todays_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSops("")
    monkeypatch.setattr(instance_config, "sops_runner", fake)

    settings = Settings(data_dir=tmp_path / "data")

    assert settings.instance_dir is None
    assert settings.resolved_instance_dir is None
    assert settings.token_store_dir == settings.resolved_data_dir / "secrets"
    assert settings.token_key is None
    assert fake.calls == []


def test_load_order_is_environment_then_instance_toml_then_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_dir = _instance(
        tmp_path,
        toml="""
        channel_name = "From Toml"
        timezone = "Europe/Paris"
        posts_per_day = 1
        """,
    )
    fake = FakeSops(
        "RUNWAY_YOUTUBE_CLIENT_ID=client-id-from-secrets\n"
        'RUNWAY_YOUTUBE_CLIENT_SECRET="yt-secret-from-sops"\n'
        "RUNWAY_INSTAGRAM_APP_ID=ig-app-from-secrets\n"
        f"RUNWAY_TOKEN_KEY={FAKE_KEY}\n"
        "OPENAI_API_KEY=sk-from-secrets\n"
    )
    monkeypatch.setattr(instance_config, "sops_runner", fake)
    monkeypatch.setenv("RUNWAY_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("RUNWAY_TIMEZONE", "UTC")
    monkeypatch.setenv("RUNWAY_INSTAGRAM_APP_ID", "ig-app-from-env")

    settings = Settings(data_dir=tmp_path / "data")

    assert settings.resolved_instance_dir == instance_dir.resolve()
    assert settings.timezone == "UTC"  # process environment beats instance.toml
    assert settings.instagram_app_id == "ig-app-from-env"  # and the secrets file
    assert settings.channel_name == "From Toml"
    assert settings.youtube_client_id == "client-id-from-secrets"
    assert settings.youtube_client_secret == "yt-secret-from-sops"
    assert settings.openai_api_key == "sk-from-secrets"
    assert settings.token_key is not None
    assert settings.token_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(settings)
    assert settings.token_store_dir == instance_dir.resolve() / "tokens"
    assert fake.calls == [
        [
            "sops",
            "--decrypt",
            "--input-type",
            "dotenv",
            "--output-type",
            "dotenv",
            str(instance_dir.resolve() / "secrets.enc.env"),
        ]
    ]


def test_instance_dir_may_come_from_init_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_dir = _instance(tmp_path, toml='channel_name = "Init Instance"\n', secrets=False)
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))

    settings = Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)

    assert settings.channel_name == "Init Instance"


def test_decrypted_secrets_are_never_written_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_dir = _instance(tmp_path)
    monkeypatch.setattr(
        instance_config,
        "sops_runner",
        FakeSops("RUNWAY_TIKTOK_CLIENT_SECRET=tt-plain-secret-value\n"),
    )

    settings = Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)

    assert settings.tiktok_client_secret == "tt-plain-secret-value"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"tt-plain-secret-value" not in path.read_bytes(), path


def test_instance_toml_rejects_secrets_and_unknown_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "instance.toml").write_text('youtube_client_secret = "plain"\n', encoding="utf-8")
    with pytest.raises(InstanceConfigError, match="secrets.enc.env") as caught:
        Settings(instance_dir=secret_dir)
    assert "plain" not in str(caught.value)

    typo_dir = tmp_path / "typo"
    typo_dir.mkdir()
    (typo_dir / "instance.toml").write_text('chanel_name = "Qlob"\n', encoding="utf-8")
    with pytest.raises(InstanceConfigError, match="chanel_name"):
        Settings(instance_dir=typo_dir)


def test_instance_dir_inside_the_code_checkout_is_refused() -> None:
    inside = Path(__file__).resolve().parents[2] / "data" / "runway-instance"
    with pytest.raises(InstanceConfigError, match="outside the Runway code checkout"):
        Settings(instance_dir=inside)


def test_missing_sops_names_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_dir = _instance(tmp_path)
    # Only git on PATH (needed to check the instance is not inside a code checkout).
    git = shutil.which("git")
    assert git is not None
    git_dir = Path(git).parent
    assert not any(git_dir.glob("sops*"))
    monkeypatch.setenv("PATH", str(git_dir))

    with pytest.raises(InstanceConfigError) as caught:
        Settings(instance_dir=instance_dir)

    message = str(caught.value)
    assert "'sops' command was not found" in message
    assert "SecretsOPerationS.SOPS" in message
    assert "FiloSottile.age" in message


def test_decryption_failure_is_explicit(tmp_path: Path) -> None:
    secrets_file = _instance(tmp_path) / "secrets.enc.env"
    fake = FakeSops("", returncode=128, stderr="Error getting data key: 0 successful groups")

    with pytest.raises(InstanceConfigError, match="could not decrypt") as caught:
        decrypt_dotenv(secrets_file, runner=fake)
    assert "age key" in str(caught.value)


def test_real_subprocess_path_runs_a_sops_binary_found_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_sops.py"
    script.write_text(
        "import sys\n"
        "expected = ['--decrypt', '--input-type', 'dotenv', '--output-type', 'dotenv']\n"
        "assert sys.argv[1:6] == expected\n"
        "print('RUNWAY_CHANNEL_NAME=From Fake Binary')\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        (bin_dir / "sops.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = bin_dir / "sops"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    secrets_file = _instance(tmp_path) / "secrets.enc.env"

    assert decrypt_dotenv(secrets_file) == {"RUNWAY_CHANNEL_NAME": "From Fake Binary"}


def test_parse_dotenv_handles_comments_quotes_and_export() -> None:
    parsed = parse_dotenv(
        "# comment\n\nexport RUNWAY_A=one\nRUNWAY_B='two words'\nRUNWAY_C=\"three\"\nRUNWAY_D=\n"
    )
    assert parsed == {
        "RUNWAY_A": "one",
        "RUNWAY_B": "two words",
        "RUNWAY_C": "three",
        "RUNWAY_D": "",
    }


def test_parse_dotenv_error_never_echoes_the_line() -> None:
    with pytest.raises(InstanceConfigError, match="line 2") as caught:
        parse_dotenv("RUNWAY_A=ok\nnot-a-pair-but-a-secret-value\n")
    assert "secret-value" not in str(caught.value)


# --- review fixes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'machine_id = "machine-a"',
        'data_dir = "D:/elsewhere"',
        'codex_cli_path = "C:/evil/codex.exe"',
        'publisher_chrome_path = "C:/evil/chrome.exe"',
        'agent_runtime = "openai"',
        'oauth_redirect_base_url = "https://attacker.example"',
        'instance_dir = "C:/other"',
    ],
)
def test_instance_toml_refuses_machine_local_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    instance_dir = _instance(tmp_path, toml=line + "\n", secrets=False)
    with pytest.raises(InstanceConfigError, match="machine-local"):
        Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)


@pytest.mark.parametrize(
    "line",
    ["publishing_enabled = true", "youtube_automation_authorized = true", "host = '::1'"],
)
def test_instance_toml_is_an_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    instance_dir = _instance(tmp_path, toml=line + "\n", secrets=False)
    with pytest.raises(InstanceConfigError, match="not allowed|machine-local"):
        Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)


def test_instance_toml_accepts_shared_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    instance_dir = _instance(
        tmp_path,
        toml="""
        channel_name = "Shared"
        timezone = "Europe/Paris"
        api_port = 8100
        web_port = 3100
        platform_provider = "mock"
        publishing_mode = "assisted"
        instance_repository = "owner/runway-instance"
        instance_snapshot_keep = 3
        """,
        secrets=False,
    )
    settings = Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)
    assert (settings.channel_name, settings.api_port, settings.instance_snapshot_keep) == (
        "Shared",
        8100,
        3,
    )


@pytest.mark.parametrize(
    "line",
    ["RUNWAY_MACHINE_ID=machine-a", "RUNWAY_CHANNEL_NAME=Qlob", "RUNWAY_DATA_DIR=D:/x"],
)
def test_secrets_file_refuses_anything_but_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(line + "\n"))
    instance_dir = _instance(tmp_path)
    with pytest.raises(InstanceConfigError, match="may not come from") as caught:
        Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)
    value = line.partition("=")[2]
    assert value not in str(caught.value)


def test_sops_errors_are_scrubbed(tmp_path: Path) -> None:
    secrets_file = _instance(tmp_path) / "secrets.enc.env"
    fake = FakeSops(
        "",
        returncode=1,
        stderr="first line\nfailed: token=abc123def456 at https://user:pw@kms.example/x?key=1",
    )
    with pytest.raises(InstanceConfigError) as caught:
        decrypt_dotenv(secrets_file, runner=fake)
    message = str(caught.value)
    assert "abc123def456" not in message
    assert "user:pw" not in message
    assert "[redacted]" in message and "[url]" in message


def test_sops_subprocess_has_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="sops", timeout=float(str(kwargs["timeout"])))

    monkeypatch.setattr(instance_config.shutil, "which", lambda _name: "sops.exe")
    monkeypatch.setattr(instance_config.subprocess, "run", fake_run)
    with pytest.raises(InstanceConfigError, match="did not finish"):
        decrypt_dotenv(_instance(tmp_path) / "secrets.enc.env")
    assert seen["timeout"] == instance_config.SOPS_TIMEOUT_SECONDS
    assert seen["shell"] is False


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/ChardXBT/Runway.git",
        "git@github-chard:ChardXBT/Runway.git",
        "ssh://git@ssh.github.com:443/chardxbt/runway",
    ],
)
def test_instance_dir_inside_any_code_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    other_checkout = tmp_path / "another-runway-clone"
    other_checkout.mkdir()
    _git(other_checkout, "init", "--quiet")
    _git(other_checkout, "remote", "add", "origin", remote)
    inside = other_checkout / "private" / "runway-instance"

    with pytest.raises(InstanceConfigError, match="checkout of the public code repository"):
        Settings(data_dir=tmp_path / "data", instance_dir=inside)


def test_instance_dir_inside_an_unrelated_repository_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "remote", "add", "origin", "https://github.com/owner/notes.git")
    instance_dir = workspace / "runway-instance"

    settings = Settings(data_dir=tmp_path / "data", instance_dir=instance_dir)
    assert settings.resolved_instance_dir == instance_dir.resolve()


def test_github_remote_parsing_is_strict() -> None:
    parse = instance_config.parse_github_remote
    assert parse("https://github.com/owner/runway-instance.git") == "owner/runway-instance"
    assert parse("git@github.com:owner/runway-instance.git") == "owner/runway-instance"
    assert parse("ssh://git@github.com/owner/runway-instance") == "owner/runway-instance"
    for url in (
        "git@github-chard:owner/runway-instance.git",
        "ssh://git@ssh.github.com:443/owner/runway-instance.git",
        "C:/repos/runway-instance",
        "https://github.com.evil.example/owner/runway-instance.git",
        "https://gitlab.com/owner/runway-instance.git",
    ):
        assert parse(url) is None, url


def test_instance_snapshot_keep_defaults_to_five(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path / "data").instance_snapshot_keep == 5


# --- p6 review fixes ----------------------------------------------------------

FAKE_FERNET = "q3mW7xK2p" + "L9vB4nR8tY1uE6iO0aS5dF2gH7jK3lZ9xC="
FAKE_YT_SECRET = "yT3sCr" + "8xK2mQ9pL4nB"
FAKE_API_VALUE = "k3y9xK2m" + "Q7pA4sD8fG"


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        (f"could not decrypt: RUNWAY_TOKEN_KEY={FAKE_FERNET}", FAKE_FERNET),
        ("bad dotenv line YOUTUBE_CLIENT_SECRET=" + FAKE_YT_SECRET, FAKE_YT_SECRET),
        ("setting MY_API_KEY: " + FAKE_API_VALUE, FAKE_API_VALUE),
        ("error: search_api_key=" + FAKE_API_VALUE, FAKE_API_VALUE),
    ],
)
def test_scrub_redacts_named_secret_values(line: str, secret: str) -> None:
    scrubbed = instance_config.scrub_tool_output(line)
    assert "[redacted]" in scrubbed
    assert secret not in scrubbed


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("error: " + "ya29" + "." + "a0AfB" + "x" * 25, "a0AfB" + "x" * 25),
        ("invalid dotenv input line: " + "GOCSPX" + "-" + "Ab3dEf6h" + "j" * 20, None),
        ("refresh " + "1" + "//" + "0gAbCd" + "f" * 30, None),
        ("token " + "rft" + "." + "AbCd" + "e" * 25, None),
        ("token " + "act" + "." + "XyZ" + "a" * 25, None),
        ("token " + "IGAA" + "AbCdEf" + "g" * 25, None),
        ("(gh" + "p_" + "0aB" * 13 + ")", "0aB" * 13),
        ("key: " + "AGE-SECRET-KEY-" + "1" + "QZ" * 29, None),
    ],
)
def test_scrub_redacts_bare_token_shapes(line: str, secret: str | None) -> None:
    scrubbed = instance_config.scrub_tool_output(line)
    assert "[redacted]" in scrubbed
    if secret is not None:
        assert secret not in scrubbed


def test_scrub_leaves_ordinary_errors_alone() -> None:
    line = "fatal: not a git repository (or any of the parent directories)"
    assert instance_config.scrub_tool_output(line) == line


def _repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet")
    if remote is not None:
        _git(path, "remote", "add", "origin", remote)
    return path


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:chardxbt/runway.git",
        "https://github.com/ChardXBT/Runway",
        "ssh://git@github.com/CHARDXBT/RUNWAY.GIT",
        "https://github.com/chardxbt/runway.git/",
    ],
)
def test_instance_repo_nested_inside_a_code_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    outer = _repo(tmp_path / "code-checkout", remote)
    inner = _repo(outer / "runway-instance")  # the instance is its own clone

    with pytest.raises(InstanceConfigError, match="checkout of the public code repository"):
        Settings(data_dir=tmp_path / "data", instance_dir=inner)


def test_a_subdirectory_of_the_nested_repo_is_also_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    outer = _repo(tmp_path / "code", "git@github.com:ChardXBT/Runway.git")
    inner = _repo(outer / "instance-clone")
    deeper = inner / "data" / "store"
    deeper.mkdir(parents=True)

    with pytest.raises(InstanceConfigError, match="checkout of the public code repository"):
        Settings(data_dir=tmp_path / "data", instance_dir=deeper)


def test_nested_repo_with_an_unrelated_remote_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_config, "sops_runner", FakeSops(""))
    outer = _repo(tmp_path / "workspace", "https://github.com/owner/notes.git")
    inner = _repo(outer / "runway-instance")

    settings = Settings(data_dir=tmp_path / "data", instance_dir=inner)
    assert settings.resolved_instance_dir == inner.resolve()
