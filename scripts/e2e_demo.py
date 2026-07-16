from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from leeway.config import Settings
from leeway.demo import run_fixture_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Leeway's network-free fixture proof.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Fresh proof directory. Defaults to data/proofs/<timestamp>.",
    )
    args = parser.parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    data_dir = args.data_dir or Path("data") / "proofs" / timestamp
    settings = Settings(data_dir=data_dir)
    result = asyncio.run(run_fixture_demo(settings))
    report_path = settings.resolved_data_dir / "reports" / "fixture-proof.json"
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Proof report: {report_path}")


if __name__ == "__main__":
    main()
