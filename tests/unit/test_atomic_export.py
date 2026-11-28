from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
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


def test_export_fdopen_failure_closes_descriptor_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "review.json"
    closed_descriptors: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    def interrupted_fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
        raise OSError("simulated fdopen interruption")

    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "fdopen", interrupted_fdopen)
    with pytest.raises(OSError, match="simulated fdopen interruption"):
        write_text_atomically(output, '{"complete": true}\n', temporary_prefix="reviewer-export-")

    assert not output.exists()
    assert len(closed_descriptors) == 1
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_export_replaces_corrupt_unicode_output_with_empty_content(tmp_path: Path) -> None:
    output = tmp_path / "missing" / "nested" / "review-ü.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"\xff\xfe corrupt partial export")

    write_text_atomically(output, "", temporary_prefix="reviewer-export-")

    assert output.read_bytes() == b""
    temporary_leftovers = [path for path in output.parent.iterdir() if path != output]
    assert temporary_leftovers == []


def test_export_creates_missing_parent_for_new_output(tmp_path: Path) -> None:
    output = tmp_path / "missing" / "nested" / "review.json"

    write_text_atomically(output, '{"complete": true}\n', temporary_prefix="reviewer-export-")

    assert output.read_text(encoding="utf-8") == '{"complete": true}\n'
    temporary_leftovers = [path for path in output.parent.iterdir() if path != output]
    assert temporary_leftovers == []


def test_export_replace_failure_allows_complete_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "review.json"
    output.write_bytes(b"\x00 corrupt partial export")
    real_replace = os.replace
    failures = 0

    def fail_once_replace(source: object, destination: object) -> None:
        nonlocal failures
        if failures == 0:
            failures += 1
            raise TimeoutError("simulated replace timeout")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_once_replace)
    with pytest.raises(TimeoutError, match="simulated replace timeout"):
        write_text_atomically(output, '{"attempt": 1}\n', temporary_prefix="reviewer-export-")

    write_text_atomically(
        output, '{"attempt": 2, "unicode": "ü"}\n', temporary_prefix="reviewer-export-"
    )

    assert output.read_text(encoding="utf-8") == '{"attempt": 2, "unicode": "ü"}\n'
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_export_huge_unicode_content_is_complete(tmp_path: Path) -> None:
    output = tmp_path / "review.json"
    content = '{"rows": "' + ("ü0" * 350000) + '"}\n'

    write_text_atomically(output, content, temporary_prefix="reviewer-export-")

    assert output.read_text(encoding="utf-8") == content
    assert len(output.read_bytes()) > 1024 * 1024
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []


def test_concurrent_exports_never_leave_partial_or_temporary_output(tmp_path: Path) -> None:
    output = tmp_path / "review.json"
    contents = [f'{{"writer": {index}, "padding": "{"ü" * 2048}"}}\n' for index in range(12)]

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(
            executor.map(
                lambda content: write_text_atomically(
                    output,
                    content,
                    temporary_prefix="reviewer-export-",
                ),
                contents,
            )
        )

    assert output.read_text(encoding="utf-8") in contents
    assert json.loads(output.read_text(encoding="utf-8"))["writer"] in range(12)
    temporary_leftovers = [path for path in tmp_path.iterdir() if path != output and path.is_file()]
    assert temporary_leftovers == []
