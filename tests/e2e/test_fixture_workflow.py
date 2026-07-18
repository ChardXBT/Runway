from pathlib import Path

import pytest

from runway.config import Settings
from runway.demo import run_fixture_demo


@pytest.mark.asyncio
async def test_complete_fixture_workflow(tmp_path: Path) -> None:
    result = await run_fixture_demo(Settings(data_dir=tmp_path / "proof"))
    assert result["status"] == "passed"
    assert all(result["assertions"].values())
