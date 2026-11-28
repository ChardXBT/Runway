"""Migration rehearsal on copies of the production database (HANDOFF section 9 step 9).

Reads the live database only through SQLite's online backup API (read-only URI),
writes two scratch copies under <scratch>/rehearsal-<stamp>/ (keep it outside every
repository: the copies are production data), then with the CANDIDATE checkout:
  A: runway db upgrade                         -> row counts of every original table unchanged
  B: runway db upgrade; runway db downgrade 0012_creator_platforms
                                               -> same tables and row counts as the original
Nothing here touches the live checkout's code, .env or processes.

Usage:
  python scripts/rehearse_migrations.py
      --source <live data dir>/runway.db
      --candidate <checkout with .venv>
      --scratch <folder outside any repo>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

DOWNGRADE_TARGET = "0012_creator_platforms"


LIVE_DB = Path()


def snapshot(db: Path) -> dict[str, object]:
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        version = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            version = row[0] if row else None
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {"version": version, "counts": counts, "integrity": integrity, "fk_violations": len(fk)}


def copy_live(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def runway(candidate: Path, python: Path, data_dir: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("RUNWAY_")}
    env["RUNWAY_DATA_DIR"] = str(data_dir)
    env["RUNWAY_INSTANCE_DIR"] = str(data_dir / "instance")
    env["RUNWAY_PLATFORM_PROVIDER"] = "mock"
    env["RUNWAY_PUBLISHING_ENABLED"] = "false"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(candidate / "src")
    # cwd is the scratch dir, so no repository .env is read.
    result = subprocess.run(
        [str(python), "-m", "runway.cli.main", *args],
        cwd=data_dir.parent,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    print(f"$ runway {' '.join(args)} -> {result.returncode}")
    tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
    for line in tail:
        print("   ", line)
    if result.returncode != 0:
        raise SystemExit(f"runway {' '.join(args)} failed")


def main() -> None:
    global LIVE_DB
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument(
        "--candidate-python",
        type=Path,
        help="Python with the candidate's dependencies (default: the candidate's .venv)",
    )
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    LIVE_DB = args.source.resolve()
    candidate = args.candidate.resolve()
    python = (
        args.candidate_python.resolve()
        if args.candidate_python is not None
        else candidate / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not LIVE_DB.is_file():
        raise SystemExit(f"no database at {LIVE_DB}")
    if not python.is_file():
        raise SystemExit(f"no Python interpreter at {python}")
    if args.scratch.exists() and any(args.scratch.iterdir()):
        print(f"scratch directory is not empty: {args.scratch}", file=sys.stderr)
        raise SystemExit(2)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = args.scratch.resolve() / f"rehearsal-{stamp}"
    a, b = base / "a" / "data" / "runway.db", base / "b" / "data" / "runway.db"
    copy_live(a)
    copy_live(b)
    original = snapshot(a)
    print(
        f"original: version={original['version']} tables={len(original['counts'])} "
        f"rows={sum(original['counts'].values())} integrity={original['integrity']}"
    )

    runway(candidate, python, a.parent, "db", "upgrade")
    upgraded = snapshot(a)
    changed_a = {
        t: (n, upgraded["counts"].get(t))
        for t, n in original["counts"].items()
        if upgraded["counts"].get(t) != n
    }
    print(
        f"A upgraded: version={upgraded['version']} tables={len(upgraded['counts'])} "
        f"integrity={upgraded['integrity']} fk_violations={upgraded['fk_violations']}"
    )
    print(f"A original tables with changed row counts: {changed_a or 'none'}")

    runway(candidate, python, b.parent, "db", "upgrade")
    runway(candidate, python, b.parent, "db", "downgrade", DOWNGRADE_TARGET)
    down = snapshot(b)
    changed_b = {
        t: (n, down["counts"].get(t))
        for t, n in original["counts"].items()
        if down["counts"].get(t) != n
    }
    extra = sorted(set(down["counts"]) - set(original["counts"]))
    missing = sorted(set(original["counts"]) - set(down["counts"]))
    print(
        f"B downgraded: version={down['version']} tables={len(down['counts'])} "
        f"integrity={down['integrity']} fk_violations={down['fk_violations']}"
    )
    print(
        f"B changed counts: {changed_b or 'none'}; extra tables: {extra or 'none'}; "
        f"missing tables: {missing or 'none'}"
    )

    report = {
        "stamp": stamp,
        "candidate": str(candidate),
        "original": original,
        "upgraded": upgraded,
        "downgraded": down,
        "changed_upgrade": changed_a,
        "changed_downgrade": changed_b,
        "extra_after_downgrade": extra,
        "missing_after_downgrade": missing,
    }
    (base / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    ok = (
        not changed_a
        and not changed_b
        and not missing
        and not extra
        and original["integrity"] == "ok"
        and upgraded["integrity"] == "ok"
        and down["integrity"] == "ok"
        and original["fk_violations"] == upgraded["fk_violations"] == down["fk_violations"]
        and original["version"] == DOWNGRADE_TARGET
        and down["version"] == DOWNGRADE_TARGET
    )
    print("REHEARSAL", "PASSED" if ok else "FAILED", "->", base / "report.json")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
