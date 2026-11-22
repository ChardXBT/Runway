"""Settings layers read from a private instance directory (``RUNWAY_INSTANCE_DIR``).

Load order, highest priority first: init arguments, process environment, the
checkout's ``.env``, ``<instance>/instance.toml`` (non-secret settings), then
``<instance>/secrets.enc.env`` decrypted by SOPS in memory. The decrypted text
is read from the ``sops`` process's stdout and never written to disk.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

INSTANCE_TOML_NAME = "instance.toml"
SECRETS_FILE_NAME = "secrets.enc.env"
TOKENS_DIR_NAME = "tokens"

SOPS_INSTALL_HINT = (
    "Install SOPS (winget install --id SecretsOPerationS.SOPS, or "
    "https://github.com/getsops/sops/releases) and age (winget install --id FiloSottile.age), "
    "then open a new terminal so they are on PATH"
)

# Settings that must come from the encrypted layer, never from plain instance.toml.
SECRET_SETTING_FIELDS = frozenset(
    {
        "openai_api_key",
        "search_api_key",
        "youtube_client_secret",
        "instagram_app_secret",
        "tiktok_client_secret",
        "token_key",
    }
)
# Resolved before the instance layers are read, so the layers cannot redefine them.
_BOOTSTRAP_FIELDS = frozenset({"instance_dir"})

SopsRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class InstanceConfigError(RuntimeError):
    """The private instance directory cannot be loaded."""


def _run_sops_subprocess(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("sops")
    if executable is None:
        raise InstanceConfigError(
            f"RUNWAY_INSTANCE_DIR contains {SECRETS_FILE_NAME} but the 'sops' command was "
            f"not found on PATH. {SOPS_INSTALL_HINT}."
        )
    return subprocess.run(
        [executable, *args[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
        check=False,
    )


# Replaced by tests with a fake; production always shells out to the real binary.
sops_runner: SopsRunner = _run_sops_subprocess


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines as SOPS writes them for ``--output-type dotenv``."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            # Never echo the line: it may hold a secret value.
            raise InstanceConfigError(
                f"{SECRETS_FILE_NAME} line {line_number} is not KEY=value after decryption"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def decrypt_dotenv(path: Path, runner: SopsRunner | None = None) -> dict[str, str]:
    """Decrypt a SOPS dotenv file in memory and return its key/value pairs."""
    active_runner = runner or sops_runner
    result = active_runner(
        ["sops", "--decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)]
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        reason = detail[-1][:300] if detail else "no error output"
        raise InstanceConfigError(
            f"sops could not decrypt {path} (exit {result.returncode}): {reason}. "
            "Check that this machine's age key is installed and listed in .sops.yaml."
        )
    return parse_dotenv(result.stdout)


def resolve_instance_dir(value: Path, project_root: Path) -> Path:
    resolved = value if value.is_absolute() else project_root / value
    resolved = resolved.resolve()
    root = project_root.resolve()
    if resolved == root or root in resolved.parents:
        raise InstanceConfigError(
            "RUNWAY_INSTANCE_DIR must be outside the Runway code checkout "
            f"({root}); a nested private clone is one 'git add -f' away from publication"
        )
    return resolved


def _field_key(name: str, field: FieldInfo) -> str:
    alias = field.validation_alias
    return alias if isinstance(alias, str) else name


def _env_name(name: str, field: FieldInfo, prefix: str) -> str:
    alias = field.validation_alias
    return alias if isinstance(alias, str) else f"{prefix}{name}"


def _load_instance_toml(path: Path, settings_cls: type[BaseSettings]) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstanceConfigError(f"{path} is not valid TOML: {exc}") from exc
    fields = settings_cls.model_fields
    unknown = sorted(key for key in payload if key not in fields)
    if unknown:
        raise InstanceConfigError(
            f"{path} has unknown settings: {', '.join(unknown)} "
            "(use Runway setting names without the RUNWAY_ prefix, e.g. channel_name)"
        )
    secret = sorted(key for key in payload if key in SECRET_SETTING_FIELDS)
    if secret:
        raise InstanceConfigError(
            f"{path} contains secret settings ({', '.join(secret)}); move them to "
            f"{SECRETS_FILE_NAME} with 'sops edit {SECRETS_FILE_NAME}'"
        )
    return {
        _field_key(name, fields[name]): value
        for name, value in payload.items()
        if name not in _BOOTSTRAP_FIELDS
    }


def _secret_values(
    decrypted: Mapping[str, str], settings_cls: type[BaseSettings]
) -> dict[str, Any]:
    prefix = str(settings_cls.model_config.get("env_prefix", ""))
    by_env_name = {key.casefold(): value for key, value in decrypted.items()}
    values: dict[str, Any] = {}
    for name, field in settings_cls.model_fields.items():
        if name in _BOOTSTRAP_FIELDS:
            continue
        env_name = _env_name(name, field, prefix).casefold()
        if env_name in by_env_name:
            values[_field_key(name, field)] = by_env_name[env_name]
    return values


class _StaticSource(PydanticBaseSettingsSource):
    """A settings layer whose values were computed once, up front."""

    def __init__(self, settings_cls: type[BaseSettings], values: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._values = values

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        key = _field_key(field_name, field)
        return self._values.get(key), key, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


def _bootstrap_instance_dir(sources: Sequence[PydanticBaseSettingsSource]) -> object | None:
    for source in sources:
        value: object = source().get("instance_dir")
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def instance_settings_sources(
    settings_cls: type[BaseSettings],
    project_root: Path,
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    """Return the instance layers (lowest priority last) or nothing when unset."""
    raw = _bootstrap_instance_dir((init_settings, env_settings, dotenv_settings))
    if raw is None:
        return ()
    instance_dir = resolve_instance_dir(Path(str(raw)), project_root)
    layers: list[PydanticBaseSettingsSource] = []
    toml_path = instance_dir / INSTANCE_TOML_NAME
    if toml_path.is_file():
        layers.append(_StaticSource(settings_cls, _load_instance_toml(toml_path, settings_cls)))
    secrets_path = instance_dir / SECRETS_FILE_NAME
    if secrets_path.is_file():
        layers.append(
            _StaticSource(settings_cls, _secret_values(decrypt_dotenv(secrets_path), settings_cls))
        )
    return tuple(layers)
