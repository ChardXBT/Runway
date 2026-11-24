"""Settings layers read from a private instance directory (``RUNWAY_INSTANCE_DIR``).

Load order, highest priority first: init arguments, process environment, the
checkout's ``.env``, ``<instance>/instance.toml`` (non-secret settings), then
``<instance>/secrets.enc.env`` decrypted by SOPS in memory. The decrypted text
is read from the ``sops`` process's stdout and never written to disk.

Both instance files are shared by every machine of the instance, so each
accepts only an explicit allow-list of settings. Machine-local settings
(identity, paths, executables, runtimes, redirect URLs) can never come from them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

INSTANCE_TOML_NAME = "instance.toml"
SECRETS_FILE_NAME = "secrets.enc.env"
TOKENS_DIR_NAME = "tokens"
CODE_REPOSITORY = "ChardXBT/Runway"
SOPS_TIMEOUT_SECONDS = 60
GIT_PROBE_TIMEOUT_SECONDS = 30

SOPS_INSTALL_HINT = (
    "Install SOPS (winget install --id SecretsOPerationS.SOPS, or "
    "https://github.com/getsops/sops/releases) and age (winget install --id FiloSottile.age), "
    "then open a new terminal so they are on PATH"
)

# instance.toml: non-secret, non-executable settings that are the same on every machine.
# Names absent from Settings (e.g. ``profile`` before it exists) are simply ignored here.
INSTANCE_TOML_FIELDS = frozenset(
    {
        "profile",
        "product_name",
        "channel_name",
        "channel_handle",
        "timezone",
        "default_post_time",
        "posts_per_day",
        "duplicate_window_days",
        "approval_required",
        "publishing_mode",
        "platform_provider",
        "api_port",
        "web_port",
        "publisher_channel_id",
        "platform_sync_interval_minutes",
        "platform_sync_max_content_items",
        "platform_sync_comment_content_limit",
        "platform_sync_analytics_days",
        "instance_repository",
        "instance_snapshot_keep",
    }
)
# secrets.enc.env: platform credentials and keys only.
SECRETS_FILE_FIELDS = frozenset(
    {
        "openai_api_key",
        "search_api_key",
        "youtube_client_id",
        "youtube_client_secret",
        "instagram_app_id",
        "instagram_app_secret",
        "tiktok_client_key",
        "tiktok_client_secret",
        "token_key",
        "token_keys",
    }
)
SECRET_SETTING_FIELDS = frozenset(
    {
        "openai_api_key",
        "search_api_key",
        "youtube_client_secret",
        "instagram_app_secret",
        "tiktok_client_secret",
        "token_key",
        "token_keys",
    }
)
# Per machine by definition; a shared file setting them would let machines impersonate
# each other or point Runway at another machine's paths and executables.
MACHINE_LOCAL_FIELDS = frozenset(
    {
        "machine_id",
        "instance_dir",
        "data_dir",
        "host",
        "codex_cli_path",
        "publisher_chrome_path",
        "agent_runtime",
        "oauth_redirect_base_url",
        "web_base_url",
    }
)

_CODE_REMOTE_PATTERN = re.compile(r"[:/]chardxbt/runway(?:\.git)?/?$", re.IGNORECASE)
_GITHUB_REMOTE_PATTERN = re.compile(
    r"^(?:https://(?:[^@/\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SCRUB_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
# NAME=value / NAME: value where the name holds a credential word, so
# RUNWAY_TOKEN_KEY=... and YOUTUBE_CLIENT_SECRET=... are redacted too.
_SCRUB_CREDENTIAL = re.compile(
    r"(?i)([a-z0-9_-]*(?:token|secret|password|key|authorization|credential|value)"
    r"[a-z0-9_-]*)(\s*[=:]\s*)\S+"
)
# Token shapes sops, gh or git may echo without a name in front of them.
_SCRUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"ya29\.[0-9A-Za-z_-]{6,}"
    r"|GOCSPX-[0-9A-Za-z_-]{6,}"
    r"|1//[0-9A-Za-z_-]{6,}"
    r"|rft\.[0-9A-Za-z!*._-]{6,}"
    r"|act\.[0-9A-Za-z!*._-]{6,}"
    r"|IG(?:AA|QV)[0-9A-Za-z_-]{6,}"
    r"|gh[pousr]_[0-9A-Za-z]{6,}"
    r"|AGE-SECRET-KEY-[0-9A-Z]{10,}"
    r")"
)

SopsRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class InstanceConfigError(RuntimeError):
    """The private instance directory cannot be loaded."""


def scrub_tool_output(text: str, limit: int = 300) -> str:
    """The last line of a tool's stderr with URLs and credential-shaped values removed."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return "no error output"
    line = _SCRUB_URL.sub("[url]", lines[-1])
    line = _SCRUB_TOKEN.sub("[redacted]", line)
    line = _SCRUB_CREDENTIAL.sub(r"\1\2[redacted]", line)
    return line[:limit]


def parse_github_remote(url: str) -> str | None:
    """``owner/name`` for a plain github.com remote URL; None for anything else.

    SSH host aliases, other ports, other hosts and local paths are deliberately
    not recognized: an unrecognized destination is never treated as verified.
    """
    match = _GITHUB_REMOTE_PATTERN.fullmatch(url.strip())
    return match.group("name") if match else None


def is_code_repository_remote(url: str) -> bool:
    """Liberal match for the public code repository, whatever the host or alias."""
    return bool(_CODE_REMOTE_PATTERN.search(url.strip()))


def _run_sops_subprocess(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("sops")
    if executable is None:
        raise InstanceConfigError(
            f"RUNWAY_INSTANCE_DIR contains {SECRETS_FILE_NAME} but the 'sops' command was "
            f"not found on PATH. {SOPS_INSTALL_HINT}."
        )
    try:
        return subprocess.run(
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=False,
            check=False,
            timeout=SOPS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstanceConfigError(
            f"sops did not finish within {SOPS_TIMEOUT_SECONDS} seconds"
        ) from exc


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
        raise InstanceConfigError(
            f"sops could not decrypt {path} (exit {result.returncode}): "
            f"{scrub_tool_output(result.stderr or '')}. "
            "Check that this machine's age key is installed and listed in .sops.yaml."
        )
    return parse_dotenv(result.stdout)


def _git_probe(git: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # Deferred: operations.instance imports this module at the top level.
    from runway.operations.instance import _isolated_env

    # Share the push/fetch isolation: injected GIT_CONFIG_* could plant a fake
    # code-repository remote here, and ambient GIT_DIR/GIT_WORK_TREE would
    # point the probe at an unrelated repository despite the -C argument.
    env = {
        name: value
        for name, value in _isolated_env().items()
        if name not in ("GIT_DIR", "GIT_WORK_TREE")
    }
    env.update({"LC_ALL": "C", "LANGUAGE": "C"})
    try:
        return subprocess.run(
            [git, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            shell=False,
            check=False,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstanceConfigError("git did not answer while checking RUNWAY_INSTANCE_DIR") from exc


def _checkout_has_code_remote(git: str, toplevel: str) -> bool:
    remotes = _git_probe(
        git, ["-C", toplevel, "config", "--get-regexp", r"^remote\..*\.(url|pushurl)$"]
    )
    for line in remotes.stdout.splitlines():
        _key, _space, url = line.partition(" ")
        if is_code_repository_remote(url):
            return True
    return False


@lru_cache(maxsize=64)
def _enclosing_code_checkout(directory: str) -> str | None:
    """The top of a git working tree of the public code repository containing ``directory``.

    Every enclosing repository is checked, not only the innermost one: a private
    clone nested inside another code checkout, or a submodule of one, is refused
    all the same.
    """
    probe = Path(directory)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    git = shutil.which("git")
    if git is None:
        raise InstanceConfigError(
            "RUNWAY_INSTANCE_DIR needs git to confirm the instance is not inside a checkout "
            "of the public code repository. Install Git (winget install --id Git.Git)."
        )
    seen: set[str] = set()
    cursor = probe
    for _ in range(16):  # a bounded walk: no filesystem nests repositories that deep
        top = _git_probe(git, ["-C", str(cursor), "rev-parse", "--show-toplevel"])
        if top.returncode != 0:
            if "not a git repository" in (top.stderr or "").lower():
                return None
            raise InstanceConfigError(
                f"cannot tell whether {directory} is inside a git checkout: "
                f"{scrub_tool_output(top.stderr or '')}"
            )
        toplevel = os.path.normpath(top.stdout.strip())
        if toplevel in seen:
            return None
        seen.add(toplevel)
        if _checkout_has_code_remote(git, toplevel):
            return toplevel
        # A submodule also reports the superproject's working tree; every other
        # nested repository is found by probing the toplevel's parent.
        super_tree = _git_probe(
            git, ["-C", toplevel, "rev-parse", "--show-superproject-working-tree"]
        )
        super_path = super_tree.stdout.strip() if super_tree.returncode == 0 else ""
        if super_path:
            cursor = Path(super_path)
            if not cursor.is_absolute():
                cursor = (Path(toplevel) / cursor).resolve()
        elif Path(toplevel).parent != Path(toplevel):
            cursor = Path(toplevel).parent
        else:
            return None
    return None


def resolve_instance_dir(value: Path, project_root: Path) -> Path:
    resolved = value if value.is_absolute() else project_root / value
    resolved = resolved.resolve()
    root = project_root.resolve()
    if resolved == root or root in resolved.parents:
        raise InstanceConfigError(
            "RUNWAY_INSTANCE_DIR must be outside the Runway code checkout "
            f"({root}); a nested private clone is one 'git add -f' away from publication"
        )
    checkout = _enclosing_code_checkout(str(resolved))
    if checkout is not None:
        raise InstanceConfigError(
            f"RUNWAY_INSTANCE_DIR is inside {checkout}, a checkout of the public code "
            f"repository {CODE_REPOSITORY}; put the private instance in its own directory"
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
    secret = sorted(key for key in payload if key in SECRET_SETTING_FIELDS)
    if secret:
        raise InstanceConfigError(
            f"{path} contains secret settings ({', '.join(secret)}); move them to "
            f"{SECRETS_FILE_NAME} with 'sops edit {SECRETS_FILE_NAME}'"
        )
    local = sorted(key for key in payload if key in MACHINE_LOCAL_FIELDS)
    if local:
        raise InstanceConfigError(
            f"{path} sets machine-local settings ({', '.join(local)}), which every machine "
            "would share; set them in this machine's environment instead"
        )
    refused = sorted(key for key in payload if key not in INSTANCE_TOML_FIELDS)
    if refused:
        known = [key for key in refused if key in fields]
        hint = (
            f"{', '.join(known)} cannot be set from the shared instance file"
            if known
            else "use Runway setting names without the RUNWAY_ prefix, e.g. channel_name"
        )
        raise InstanceConfigError(
            f"{path} has settings that are not allowed there: {', '.join(refused)} ({hint})"
        )
    return {
        _field_key(name, fields[name]): value for name, value in payload.items() if name in fields
    }


def _secret_values(
    decrypted: Mapping[str, str], settings_cls: type[BaseSettings]
) -> dict[str, Any]:
    prefix = str(settings_cls.model_config.get("env_prefix", ""))
    fields_by_env = {
        _env_name(name, field, prefix).casefold(): (name, field)
        for name, field in settings_cls.model_fields.items()
    }
    values: dict[str, Any] = {}
    refused: list[str] = []
    for key, value in decrypted.items():
        match = fields_by_env.get(key.casefold())
        if match is None or match[0] not in SECRETS_FILE_FIELDS:
            refused.append(key)
            continue
        name, field = match
        values[_field_key(name, field)] = value
    if refused:
        # Key names only; never a value.
        raise InstanceConfigError(
            f"{SECRETS_FILE_NAME} sets {', '.join(sorted(refused))}, which may not come from "
            "the shared secrets file (only platform credentials and keys are allowed; "
            "machine identity and paths belong in this machine's environment)"
        )
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
