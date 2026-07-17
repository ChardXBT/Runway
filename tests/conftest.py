from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from leeway.config import Settings
from leeway.db import Database, initialize_database


@pytest.fixture(autouse=True)
def force_offline_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated tests must never consume ChatGPT/Codex or paid API usage."""
    monkeypatch.setenv("LEWAY_AGENT_RUNTIME", "mock")
    monkeypatch.setenv("LEWAY_PUBLISHING_ENABLED", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture()
def database(settings: Settings) -> Generator[Database, None, None]:
    db = initialize_database(settings)
    yield db
    db.engine.dispose()
