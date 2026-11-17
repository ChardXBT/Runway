from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event_type", "entity_type", "entity_id", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# httpx logs every request URL at INFO ("HTTP Request: GET https://..."). Some
# provider grant endpoints require credentials as query parameters (Instagram's
# ig_exchange_token takes client_secret and access_token), so those lines would
# write secrets to the service logs. Request-level logs from the HTTP stack are
# kept at WARNING and above; provider failures are logged through
# safe_error_summary instead.
URL_LOGGING_LIBRARIES = ("httpx", "httpcore")


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
