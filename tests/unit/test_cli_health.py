from __future__ import annotations

from typing import Any

import pytest

from runway.cli import main as cli_main


class FakeResponse:
    status = 200

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"product":"Runway"}'


def test_running_runway_api_is_a_healthy_port_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_main.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    assert cli_main._runway_api_healthy("127.0.0.1", 8000) is True


def test_non_runway_response_does_not_claim_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse()
    monkeypatch.setattr(response, "read", lambda: b'{"product":"something-else"}')

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        return response

    monkeypatch.setattr(cli_main.urllib.request, "urlopen", fake_urlopen)

    assert cli_main._runway_api_healthy("127.0.0.1", 8000) is False


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("v24.14.0", True),
        ("v22.23.5", True),
        ("v20.9.0", True),
        ("v20.19.1-nightly2026", True),
        # Next.js 16 refuses to build or start on these, so doctor must not report OK.
        ("v20.8.1", False),
        ("v18.20.4", False),
        ("available", False),
        ("", False),
        (None, False),
    ],
)
def test_doctor_requires_the_node_release_next_needs(version: str | None, supported: bool) -> None:
    assert cli_main._node_version_supported(version) is supported


def test_minimum_node_matches_the_installed_next_engines_field() -> None:
    import json
    from pathlib import Path

    next_package = Path(__file__).resolve().parents[2] / "apps/web/node_modules/next/package.json"
    if not next_package.exists():
        pytest.skip("apps/web dependencies are not installed")
    engines = json.loads(next_package.read_text(encoding="utf-8"))["engines"]["node"]
    minimum = ".".join(str(part) for part in cli_main.MINIMUM_NODE_VERSION)
    assert engines == f">={minimum}"
