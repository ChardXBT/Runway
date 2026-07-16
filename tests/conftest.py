from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from leeway.config import Settings
from leeway.db import Database, initialize_database


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture()
def database(settings: Settings) -> Generator[Database, None, None]:
    db = initialize_database(settings)
    yield db
    db.engine.dispose()
