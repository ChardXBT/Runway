"""No test may hard-code the migration head (review item 5).

The head moves with every new revision; a test that asserts a literal head
breaks on the next migration (CI run 45062684012 failed that way). Tests take
the head from Alembic's ScriptDirectory, e.g. `runway.db.bootstrap.migration_head`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"


def _script() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def literal_head_uses(source: str, head: str) -> list[int]:
    """Lines where `head` appears as a literal in an assert or a *HEAD* constant."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assert):
            for inner in ast.walk(node.test):
                if isinstance(inner, ast.Constant) and inner.value == head:
                    lines.append(inner.lineno)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if node.value.value == head and any("HEAD" in name.upper() for name in names):
                lines.append(node.lineno)
    return sorted(lines)


def test_the_detector_finds_a_literal_head_assertion() -> None:
    sample = (
        'HEAD = "0099_future"\n'
        "def test_x(migration):\n"
        '    assert migration == "0099_future"\n'
        '    assert migration != "0012_creator_platforms"\n'
        '    command.upgrade(config, "0099_future")\n'
    )
    assert literal_head_uses(sample, "0099_future") == [1, 3]


def test_no_test_asserts_a_literal_migration_head() -> None:
    head = _script().get_current_head()
    assert head is not None
    offenders = {
        str(path.relative_to(REPO_ROOT)): lines
        for path in sorted(TESTS.rglob("*.py"))
        if path != Path(__file__).resolve()
        and (lines := literal_head_uses(path.read_text(encoding="utf-8"), head))
    }
    assert offenders == {}, (
        f"assert the head via migration_head()/ScriptDirectory, not {head!r}: {offenders}"
    )
