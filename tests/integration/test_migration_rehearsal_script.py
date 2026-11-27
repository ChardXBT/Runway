"""End-to-end coverage for the production-copy migration rehearsal script."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config, migration_head

NOW = "2026-10-01 00:00:00.000000"
DST_FOLD = "2026-11-01 02:30:00.000000"
DST_GAP = "2026-03-08 02:30:00.000000"


def _generated_source(data_dir: Path) -> Path:
    """A production-shaped 0012 database with duplicate, orphan and timezone rows."""
    settings = Settings(data_dir=data_dir)
    settings.ensure_directories()
    config = alembic_config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, "0012_creator_platforms")
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.executescript(
            f"""
            INSERT INTO channels (
                id, name, handle, youtube_channel_id, timezone, default_post_time,
                planning_horizon_days, duplicate_window_days, created_at, updated_at
            ) VALUES (
                1, 'Qlob', 'Qlob', NULL, 'America/Toronto', '10:00', 7, 180,
                '{NOW}', '{NOW}'
            );
            INSERT INTO connected_accounts (
                id, platform, external_account_id, handle, status, granted_scopes_json,
                token_ref, connected_at, metadata_json, created_at, updated_at
            ) VALUES (
                1, 'youtube', 'synthetic-account', 'synthetic', 'connected', '[]',
                'synthetic-token-ref', '{NOW}', '{{}}', '{NOW}', '{NOW}'
            );
            INSERT INTO platform_content (
                id, account_id, platform, external_id, content_type, title, caption,
                published_at, metrics_json, first_seen_at, last_seen_at, raw_json
            ) VALUES
                (1, 1, 'youtube', 'dup-video-a', 'video', 'Same title', 'Same caption',
                 '{DST_FOLD}', '{{}}', '{NOW}', '{NOW}', '{{}}'),
                (2, 1, 'youtube', 'dup-video-b', 'video', 'Same title', 'Same caption',
                 '{DST_GAP}', '{{}}', '{NOW}', '{NOW}', '{{}}');
            INSERT INTO content_metric_snapshots (id, content_id, captured_at, metrics_json)
            VALUES (1, 999, '{NOW}', '{{}}');
            INSERT INTO comment_records (
                id, account_id, platform, external_id, content_id, author_is_owner, text,
                status, has_owner_reply, raw_json, first_seen_at, last_seen_at
            ) VALUES (1, 1, 'youtube', 'orphan-comment', 999, 0, 'orphan', 'new', 0,
                '{{}}', '{NOW}', '{NOW}');
            INSERT INTO reply_drafts (id, comment_id, text, updated_at)
            VALUES (1, 999, 'orphan reply', '{NOW}');
            INSERT INTO content_drafts (
                id, title, body, scheduled_for, status, created_at, updated_at
            ) VALUES (1, 'timezone draft', 'body', '{DST_GAP}', 'scheduled', '{NOW}', '{NOW}');
            INSERT INTO draft_targets (
                id, draft_id, platform, account_id, settings_json, status, published_content_id
            ) VALUES (1, 1, 'youtube', 1, '{{}}', 'pending', 999);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return settings.database_path


def test_migration_rehearsal_script_passes_on_generated_edge_data(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    source = _generated_source(tmp_path / "source-data")
    scratch = tmp_path / "scratch"
    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "rehearse_migrations.py"),
            "--source",
            str(source),
            "--candidate",
            str(repo),
            "--candidate-python",
            sys.executable,
            "--scratch",
            str(scratch),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REHEARSAL PASSED" in result.stdout
    reports = list(scratch.glob("rehearsal-*/report.json"))
    assert len(reports) == 1
    report_text = reports[0].read_text(encoding="utf-8")
    assert "Same title" not in report_text
    assert "orphan reply" not in report_text
    report = json.loads(report_text)
    assert report["original"]["version"] == "0012_creator_platforms"
    assert report["upgraded"]["version"] == migration_head(Settings())
    assert report["downgraded"]["version"] == "0012_creator_platforms"
    assert report["changed_upgrade"] == {}
    assert report["changed_downgrade"] == {}
    assert report["missing_after_downgrade"] == []
    assert report["extra_after_downgrade"] == []
    assert report["upgraded"]["fk_violations"] == 4
    assert report["downgraded"]["fk_violations"] == 4
    assert report["downgraded"]["integrity"] == "ok"


def test_migration_rehearsal_script_refuses_a_nonempty_scratch_directory(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    source = _generated_source(tmp_path / "source-data")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "keep.db").write_bytes(b"do not delete")
    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "rehearse_migrations.py"),
            "--source",
            str(source),
            "--candidate",
            str(repo),
            "--candidate-python",
            sys.executable,
            "--scratch",
            str(scratch),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "scratch directory is not empty" in result.stderr
    assert (scratch / "keep.db").read_bytes() == b"do not delete"
