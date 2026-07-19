from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def write_markdown(path: Path, title: str, payload: dict[str, Any]) -> Path:
    lines = [f"# {title}", ""]
    for key, value in payload.items():
        label = key.replace("_", " ").title()
        if isinstance(value, (dict, list)):
            lines.extend(
                [
                    f"## {label}",
                    "",
                    "```json",
                    json.dumps(value, indent=2, sort_keys=True, default=str),
                    "```",
                    "",
                ]
            )
        else:
            lines.extend([f"- **{label}:** {value}", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
