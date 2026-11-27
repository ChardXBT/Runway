from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runway.config import Settings
from runway.db.base import Database
from runway.platforms.base import TokenSet
from runway.platforms.registry import ProviderRegistry

_CHILD_SCRIPT = """
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runway.config import Settings
from runway.db.base import Database
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.mock import MockPlatformProvider
from runway.platforms.registry import ProviderRegistry


data_dir = Path(sys.argv[1])
token_ref = sys.argv[2]
attempts_path = Path(sys.argv[3])
result_path = Path(sys.argv[4])
worker = sys.argv[5]

settings = Settings(data_dir=data_dir, platform_provider="mock")
database = Database(settings)
registry = ProviderRegistry(database, settings)


class RacingRefresh(MockPlatformProvider):
    def refresh(self, token: TokenSet) -> TokenSet:
        with attempts_path.open("a", encoding="utf-8") as handle:
            handle.write(worker + "\\n")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                count = len(attempts_path.read_text(encoding="utf-8").splitlines())
            except FileNotFoundError:
                count = 0
            if count >= 2:
                break
            time.sleep(0.01)
        return token.model_copy(
            update={
                "access_token": "fresh-access-" + worker,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        )


registry.set_provider(Platform.youtube, RacingRefresh(Platform.youtube, settings))
try:
    token = registry.fresh_token(Platform.youtube, token_ref)
    result_path.write_text(token.access_token, encoding="utf-8")
finally:
    database.engine.dispose()
"""


def test_one_provider_refresh_occurs_across_independent_processes(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = Database(settings)
    registry = ProviderRegistry(database, settings)
    token_ref = registry.token_store.new_token_ref()
    registry.token_store.store(
        token_ref,
        TokenSet(
            access_token="old-access",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    database.engine.dispose()

    attempts_path = tmp_path / "refresh-attempts.txt"
    script_path = tmp_path / "refresh_child.py"
    script_path.write_text(textwrap.dedent(_CHILD_SCRIPT), encoding="utf-8")
    result_paths = [tmp_path / f"result-{index}.txt" for index in range(2)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(script_path),
                str(settings.resolved_data_dir),
                token_ref,
                str(attempts_path),
                str(result_paths[index]),
                str(index),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]

    outputs: list[tuple[int, str, str]] = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"token refresh child timed out\nstdout={stdout}\nstderr={stderr}"
            ) from None
        outputs.append((process.returncode, stdout, stderr))

    assert outputs == [(0, "", ""), (0, "", "")]
    attempts = attempts_path.read_text(encoding="utf-8").splitlines()
    assert attempts in ([str(0)], [str(1)])

    returned_tokens = [path.read_text(encoding="utf-8") for path in result_paths]
    assert returned_tokens[0] == returned_tokens[1]
    assert returned_tokens[0].startswith("fresh-access-")
    stored = registry.token_store.load(token_ref)
    assert stored is not None
    assert stored.access_token == returned_tokens[0]
