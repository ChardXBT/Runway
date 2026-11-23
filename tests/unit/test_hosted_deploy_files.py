"""The hosted image, compose file and restore script keep their hardening (spec 05 section 7)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")
DEV_TOOLS = {"pytest", "mypy", "pip-audit", "ruff", "pytest-asyncio", "pytest-cov", "coverage"}
# requirements.lock is resolved on Windows; the image lock is resolved for Linux,
# where uvicorn[standard] adds these. Any other difference is drift.
LINUX_ONLY = {"uvloop"}


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if match:
            pins[match.group(1).casefold().replace("_", "-")] = match.group(2)
    return pins


def test_base_images_are_pinned_by_digest() -> None:
    for name in ("Dockerfile", "deploy/web.Dockerfile"):
        froms = [
            line
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ")
        ]
        assert froms, name
        for line in froms:
            assert DIGEST.search(line), (name, line)
    compose = yaml.safe_load((ROOT / "deploy/compose.hosted.yml").read_text(encoding="utf-8"))
    assert DIGEST.search(compose["services"]["postgres"]["image"])


def test_the_image_installs_the_hashed_runtime_lock_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes --requirement requirements-runtime.lock" in dockerfile
    assert "requirements.lock" not in dockerfile.replace("requirements-runtime.lock", "")

    runtime_text = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")
    runtime = _pins(ROOT / "requirements-runtime.lock")
    full = _pins(ROOT / "requirements.lock")
    assert len(runtime) > 30
    assert not DEV_TOOLS & set(runtime)
    # Same versions as the full lock: regenerate one when the other changes.
    assert set(runtime) - set(full) <= LINUX_ONLY
    shared = set(runtime) & set(full)
    assert {name: full[name] for name in shared} == {name: runtime[name] for name in shared}
    # Every pinned requirement carries at least one hash.
    entries = [entry for entry in re.split(r"\n(?=\S)", runtime_text) if "==" in entry]
    assert len(entries) == len(runtime)
    assert all("--hash=sha256:" in entry for entry in entries)


def test_the_healthcheck_probes_the_configured_api_port() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    healthcheck = dockerfile[dockerfile.index("HEALTHCHECK") :]
    healthcheck = healthcheck[: healthcheck.index("\n\n")]
    assert "RUNWAY_API_PORT" in healthcheck
    assert ":8000/" not in healthcheck


def _environment(compose: dict[str, Any], service: str) -> set[str]:
    environment = compose["services"][service]["environment"]
    return set(environment)


def test_each_compose_service_gets_only_its_secrets() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.hosted.yml").read_text(encoding="utf-8"))
    platform = {
        "RUNWAY_TOKEN_KEYS",
        "RUNWAY_YOUTUBE_CLIENT_SECRET",
        "RUNWAY_INSTAGRAM_APP_SECRET",
        "RUNWAY_TIKTOK_CLIENT_SECRET",
    }
    webhook = {"RUNWAY_CLERK_WEBHOOK_SECRET"}
    api, worker, migrate = (_environment(compose, name) for name in ("api", "worker", "migrate"))
    assert webhook <= api and not platform & api
    assert platform <= worker and not webhook & worker
    assert not (platform | webhook) & migrate
    assert "RUNWAY_DB_OWNER_PASSWORD" not in "".join(
        str(compose["services"][name]["environment"]) for name in ("api", "worker")
    )
    # X-Forwarded-For is trusted from the proxy's gateway only, not a /12.
    common = compose["x-runway-env"]
    assert common["RUNWAY_FORWARDED_ALLOW_IPS"].endswith(":-172.30.0.1}")
    gateway = compose["networks"]["default"]["ipam"]["config"][0]["gateway"]
    assert gateway == "172.30.0.1"


def _bash() -> str | None:
    found = shutil.which("bash")
    # WSL's launcher in System32 is not a usable POSIX shell for this script.
    if found is None or "system32" in found.casefold():
        git_bash = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git/bin/bash.exe"
        return str(git_bash) if git_bash.exists() else None
    return found


def _restore_latest(tmp_path: Path, remote_files: list[str], **env: str) -> tuple[int, str]:
    bash = _bash()
    if bash is None:
        pytest.skip("bash is not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    picked = tmp_path / "picked.txt"
    (bin_dir / "rclone").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = lsf ]; then printf "%s\\n" ' + " ".join(remote_files) + "; exit 0; fi\n"
        f'if [ "$1" = copyto ]; then echo "$2" >> "{picked.as_posix()}"; exit 1; fi\n'
        "exit 3\n",
        encoding="utf-8",
        newline="\n",
    )
    (bin_dir / "psql").write_text("#!/usr/bin/env bash\necho 0\n", encoding="utf-8", newline="\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    command = (
        'bin="$RUNWAY_TEST_BIN"; '
        'if command -v cygpath >/dev/null; then bin="$(cygpath -u "$bin")"; fi; '
        'export PATH="$bin:$PATH"; bash "$RUNWAY_TEST_SCRIPT" latest'
    )
    environment = {
        **os.environ,
        "PGDATABASE": "runway",
        "RUNWAY_RESTORE_CONFIRM": "runway",
        "RUNWAY_BACKUP_REMOTE": "r2:backups",
        "RUNWAY_TEST_BIN": bin_dir.as_posix(),
        "RUNWAY_TEST_SCRIPT": (ROOT / "deploy/restore.sh").as_posix(),
        **env,
    }
    result = subprocess.run(
        [bash, "-c", command],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )
    chosen = picked.read_text(encoding="utf-8").strip() if picked.exists() else ""
    return result.returncode, chosen + "|" + result.stderr


def test_restore_latest_only_picks_dumps_of_the_target_database(tmp_path: Path) -> None:
    files = [
        "runway-runway-20261120T031700Z.dump",
        "runway-runway-20261120T031700Z.dump.sha256",
        "runway-staging-20261122T031700Z.dump",
    ]
    code, output = _restore_latest(tmp_path / "prod", files)
    assert output.startswith("r2:backups/runway-runway-20261120T031700Z.dump"), output
    assert code != 0  # the fake copy fails right after the choice

    code, output = _restore_latest(tmp_path / "only-other", files[2:])
    assert code == 1 and "no backups of runway" in output, output

    code, output = _restore_latest(
        tmp_path / "rehearsal",
        files,
        PGDATABASE="runway_rehearsal",
        RUNWAY_RESTORE_CONFIRM="runway_rehearsal",
        RUNWAY_RESTORE_SOURCE_DB="staging",
    )
    assert output.startswith("r2:backups/runway-staging-20261122T031700Z.dump"), output
