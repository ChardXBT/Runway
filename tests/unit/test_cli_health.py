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
        return b'{"product":"RunWay"}'


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
