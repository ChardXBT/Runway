from __future__ import annotations

import os
from pathlib import Path

import pytest

from runway.intelligence.atomic_export import write_text_atomically


class InterruptedWriter:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.closed = False

    def __enter__(self) -> InterruptedWriter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def write(self, content: str) -> int:
        os.write(self.descriptor, content.encode("utf-8")[:8])
        raise OSError("simulated write interruption")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


@pytest.mark.parametrize("has_existing_export", [False, True])
def test_export_write_interruption_preserves_final_path_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_existing_export: bool,
) -> None:
    output = tmp_path / "review.json"
    if has_existing_export:
        output.write_text("complete prior export\n", encoding="utf-8")

    writers: list[InterruptedWriter] = []

    def interrupted_fdopen(descriptor: int, *args: object, **kwargs: object) -> InterruptedWriter:
        writer = InterruptedWriter(descriptor)
        writers.append(writer)
        return writer

    monkeypatch.setattr(os, "fdopen", interrupted_fdopen)
    with pytest.raises(OSError, match="simulated write interruption"):
        write_text_atomically(output, '{"partial":', temporary_prefix="reviewer-export-")

    if has_existing_export:
        assert output.read_text(encoding="utf-8") == "complete prior export\n"
    else:
        assert not output.exists()
    assert writers[0].closed
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []
