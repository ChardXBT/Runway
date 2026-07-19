from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from runway.config import Settings
from runway.publishing import youtube as youtube_module
from runway.publishing.youtube import PlaywrightYouTubeAdapter


def _settings(tmp_path: Path, chrome: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        publisher_chrome_path=chrome,
        publisher_browser_channel="chrome",
    )


def test_publisher_login_opens_normal_chrome_with_isolated_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"fixture")
    adapter = PlaywrightYouTubeAdapter(_settings(tmp_path, chrome))
    calls: list[list[str]] = []

    monkeypatch.setattr(
        youtube_module.subprocess,
        "Popen",
        lambda command: calls.append(command),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    adapter.login_interactive()

    assert len(calls) == 1
    command = calls[0]
    assert command[0] == str(chrome.resolve())
    assert f"--user-data-dir={adapter.settings.publisher_profile_dir}" in command
    assert "--remote-debugging-port" not in " ".join(command)
    assert command[-1] == adapter.settings.publisher_channel_url


def test_guarded_publisher_reopens_the_profile_with_installed_chrome(
    tmp_path: Path,
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"fixture")
    adapter = PlaywrightYouTubeAdapter(_settings(tmp_path, chrome))
    captured: dict[str, Any] = {}

    class Chromium:
        def launch_persistent_context(self, **kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

    class Playwright:
        chromium = Chromium()

    adapter._launch_publisher_context(Playwright())

    assert captured["channel"] == "chrome"
    assert captured["user_data_dir"] == str(adapter.settings.publisher_profile_dir)
    assert captured["headless"] is False


def test_publisher_selects_the_only_visible_control_from_responsive_duplicates() -> None:
    class Item:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        def is_visible(self) -> bool:
            return self.visible

    class Locator:
        def __init__(self, items: list[Item]) -> None:
            self.items = items

        def count(self) -> int:
            return len(self.items)

        def nth(self, index: int) -> Item:
            return self.items[index]

    hidden = Item(False)
    visible = Item(True)

    selected = PlaywrightYouTubeAdapter._one_visible(
        Locator([hidden, visible]),
        "schedule action menu",
    )

    assert selected is visible


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://www.youtube.com/@creator/videos",
            "https://www.youtube.com/@creator/posts",
        ),
        (
            "@creator",
            "https://www.youtube.com/@creator/posts",
        ),
        (
            "UCQ-nHijGwxNU3Go_wyLQ5Ng",
            "https://www.youtube.com/channel/UCQ-nHijGwxNU3Go_wyLQ5Ng/posts",
        ),
    ],
)
def test_connector_channel_url_is_restricted_to_youtube(
    value: str,
    expected: str,
) -> None:
    assert PlaywrightYouTubeAdapter._normalize_connector_channel_url(value) == expected


def test_connector_channel_url_rejects_external_hosts() -> None:
    with pytest.raises(ValueError, match="youtube.com"):
        PlaywrightYouTubeAdapter._normalize_connector_channel_url("https://example.com/@creator")
