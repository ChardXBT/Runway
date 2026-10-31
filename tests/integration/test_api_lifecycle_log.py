from __future__ import annotations

import importlib
import logging

from fastapi.testclient import TestClient

from runway.config import Settings


def test_api_start_and_stop_are_logged_with_mode_and_no_secrets(
    settings: Settings,
) -> None:
    # The migration runner inside create_app loads alembic.ini through
    # fileConfig; the application must keep its loggers alive and install its
    # own configuration afterwards, so capture on the app logger itself rather
    # than relying on root handlers surviving startup.
    app_module = importlib.import_module("runway.api.app")
    secret = "client-secret-value-must-not-appear"
    configured = settings.model_copy(
        update={
            "youtube_client_id": "client-id-value",
            "youtube_client_secret": secret,
            "instagram_app_secret": secret,
            "tiktok_client_secret": secret,
            "openai_api_key": secret,
            "search_api_key": secret,
        }
    )
    app = app_module.create_app(configured)

    logger = logging.getLogger("runway.api.app")
    assert not logger.disabled, "app logger must not be disabled by migration logging"
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            assert client.get("/health").status_code == 200
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    starts = [r for r in records if getattr(r, "event_type", None) == "api_start"]
    stops = [r for r in records if getattr(r, "event_type", None) == "api_stop"]
    assert len(starts) == 1
    assert len(stops) == 1
    message = starts[0].getMessage()
    assert message.startswith("runway api start ")
    for fragment in (
        "version=",
        "git=",
        "platform_provider=",
        "publishing_enabled=False",
        "publishing_mode=assisted",
        "inbox_replies_enabled=False",
        "agent_runtime=",
        "sync_interval_minutes=0",
    ):
        assert fragment in message
    rendered = " ".join(record.getMessage() for record in records)
    assert secret not in rendered
    assert "client-id-value" not in rendered
