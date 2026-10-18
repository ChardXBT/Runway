from __future__ import annotations

import json

from runway.config import Settings


def test_public_dict_never_exposes_secret_values(tmp_path) -> None:
    secrets = {
        "youtube_client_secret": "yt-secret-value-12345",
        "instagram_app_secret": "ig-secret-value-12345",
        "tiktok_client_secret": "tt-secret-value-12345",
        "openai_api_key": "sk-openai-12345",
        "search_api_key": "search-key-12345",
        "codex_access_token": "codex-token-12345",
        "codex_api_key": "codex-key-12345",
    }
    settings = Settings(
        data_dir=tmp_path / "data",
        youtube_client_id="yt-client-id",
        instagram_app_id="ig-app-id",
        tiktok_client_key="tt-client-key",
        **secrets,
    )
    public = settings.public_dict()
    rendered = json.dumps(public)
    for label, value in secrets.items():
        assert value not in rendered, f"{label} leaked into public settings"
    assert public["youtube_oauth_configured"] is True
    assert public["instagram_oauth_configured"] is True
    assert public["tiktok_oauth_configured"] is True
    assert isinstance(public["openai_configured"], bool)
    for key, value in public.items():
        assert not isinstance(value, str) or not any(
            marker in key for marker in ("secret", "token", "api_key")
        ), f"string value under suspicious key {key}"
