from __future__ import annotations

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
        "RUNWAY_CHANNEL_NAME=From Secrets\n"
        "RUNWAY_TIMEZONE=Asia/Tokyo\n"
        "RUNWAY_YOUTUBE_CLIENT_ID=client-id-from-secrets\n"
        'RUNWAY_YOUTUBE_CLIENT_SECRET="yt-secret-from-sops"\n'
        f"RUNWAY_TOKEN_KEY={FAKE_KEY}\n"
        "OPENAI_API_KEY=sk-from-secrets\n"
    )
    monkeypatch.setattr(instance_config, "sops_runner", fake)
    monkeypatch.setenv("RUNWAY_INSTANCE_DIR", str(instance_dir))
    monkeypatch.setenv("RUNWAY_TIMEZONE", "UTC")

    settings = Settings(data_dir=tmp_path / "data")

    assert settings.resolved_instance_dir == instance_dir.resolve()
    assert settings.timezone == "UTC"  # process environment wins
    assert settings.channel_name == "From Toml"  # instance.toml beats secrets
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
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

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
