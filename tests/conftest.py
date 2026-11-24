from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

# API modules build the application at import time (without touching the database). Point
# the settings at a disposable directory before any repository modules can read a .env file.
_COLLECTION_DATA_DIR = tempfile.mkdtemp(prefix="runway-pytest-collection-")
os.environ["RUNWAY_DATA_DIR"] = _COLLECTION_DATA_DIR
os.environ["RUNWAY_AGENT_RUNTIME"] = "mock"
os.environ["RUNWAY_PUBLISHING_ENABLED"] = "false"

from runway.config import Settings  # noqa: E402
from runway.db import Database, initialize_database  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    # The backend-postgres CI job runs the suite against a service database;
    # locally this stays unset and the `postgres`-marked tests skip.
    parser.addoption("--postgres-dsn", action="store", default=None)


@pytest.fixture(autouse=True)
def force_offline_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated tests must never consume ChatGPT/Codex or paid API usage."""
    monkeypatch.setenv("RUNWAY_AGENT_RUNTIME", "mock")
    monkeypatch.setenv("RUNWAY_PUBLISHING_ENABLED", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)


@pytest.fixture()
def settings(tmp_path: Path) -> Generator[Settings, None, None]:
    """A disposable settings dir; a per-test Postgres schema when configured.

    With RUNWAY_TEST_DATABASE_URL set (the backend-postgres CI job), every
    test migrates and uses its own schema in the shared service database, so
    the suite exercises the Postgres dialect without cross-test leakage.
    """
    base = Settings(
        data_dir=tmp_path / "data",
        discovery_secondary_topics="",
    )
    dsn = os.environ.get("RUNWAY_TEST_DATABASE_URL")
    if not dsn:
        yield base
        return
    import uuid
    from urllib.parse import quote

    from pydantic import SecretStr
    from sqlalchemy import create_engine, text

    schema = f"t_{uuid.uuid4().hex[:16]}"
    engine = create_engine(dsn)
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
    separator = "&" if "?" in dsn else "?"
    yield base.model_copy(
        update={
            "database_dsn": SecretStr(
                f"{dsn}{separator}options={quote('-c search_path=' + schema)}"
            )
        }
    )
    with engine.connect() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        connection.commit()
    engine.dispose()


@pytest.fixture()
def database(settings: Settings) -> Generator[Database, None, None]:
    db = initialize_database(settings)
    yield db
    db.engine.dispose()
