from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

# API modules create the application at import time. Force that initialization into a
# disposable directory before any repository modules can read the developer's .env file.
_COLLECTION_DATA_DIR = tempfile.mkdtemp(prefix="runway-pytest-collection-")
os.environ["RUNWAY_DATA_DIR"] = _COLLECTION_DATA_DIR
os.environ["RUNWAY_AGENT_RUNTIME"] = "mock"
os.environ["RUNWAY_PUBLISHING_ENABLED"] = "false"

from runway.config import Settings  # noqa: E402
from runway.db import Database, initialize_database  # noqa: E402


@pytest.fixture(autouse=True)
def force_offline_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated tests must never consume ChatGPT/Codex or paid API usage."""
    monkeypatch.setenv("RUNWAY_AGENT_RUNTIME", "mock")
    monkeypatch.setenv("RUNWAY_PUBLISHING_ENABLED", "false")
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
