from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leeway.analysis import runtime as runtime_module
from leeway.analysis.runtime import (
    AgentUsageLimitReached,
    CodexAgentRuntime,
    PaidApiAuthenticationBlocked,
    runtime_for,
)
from leeway.analysis.schemas import (
    CandidateAnalysis,
    CaptionOptions,
    HistoricalAnnotation,
    SearchPlan,
    StyleSummary,
)
from leeway.config import Settings


def _candidate_output() -> dict[str, object]:
    return {
        "franchise": "Fixture Show",
        "characters": ["Fixture Character"],
        "scene_archetype": "reaction",
        "composition": "centered",
        "emotion": "surprise",
        "text_overlay": False,
        "watermark_probability": 0.01,
        "unsafe_probability": 0.0,
        "personal_artwork_probability": 0.0,
        "fan_art_probability": 0.0,
        "caption_potential": 0.82,
        "confidence": 0.9,
    }


def _assert_strict_object_schemas(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_object_schemas(item)
        return
    if not isinstance(value, dict):
        return
    properties = value.get("properties")
    if value.get("type") == "object" and isinstance(properties, dict):
        assert value.get("additionalProperties") is False
        assert set(value.get("required", [])) == set(properties)
    for item in value.values():
        _assert_strict_object_schemas(item)


@pytest.mark.parametrize(
    "schema",
    [HistoricalAnnotation, StyleSummary, SearchPlan, CandidateAnalysis, CaptionOptions],
)
def test_codex_output_schemas_require_every_property(schema: type[Any]) -> None:
    _assert_strict_object_schemas(schema.model_json_schema())


@pytest.mark.asyncio
async def test_codex_runtime_uses_chatgpt_guardrails_and_real_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_runtime="codex",
        codex_model="gpt-5.6-luna",
        codex_reasoning_effort="low",
    )
    image = tmp_path / "candidate.png"
    image.write_bytes(b"fixture-image")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-leak")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Logged in using ChatGPT\n",
                stderr="",
            )
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(_candidate_output()), encoding="utf-8")
        usage_event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 8},
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(usage_event),
            stderr="",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)
    runtime = CodexAgentRuntime(settings, command_prefix=["codex-test"])
    result = await runtime.analyze_candidate_image({"candidate_id": 7, "_image_path": str(image)})

    assert result.caption_potential == 0.82
    assert runtime.last_token_usage["input_tokens"] == 12
    exec_command, exec_kwargs = calls[1]
    assert exec_command[:4] == ["codex-test", "--ask-for-approval", "never", "exec"]
    assert exec_command[exec_command.index("--sandbox") + 1] == "read-only"
    assert exec_command[exec_command.index("--model") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in exec_command
    assert 'web_search="disabled"' in exec_command
    assert exec_command[exec_command.index("--image") + 1] == str(image.resolve())
    assert exec_command[-1] == "-"
    assert Path(exec_kwargs["cwd"]).parent.name == "codex-runtime"
    assert "OPENAI_API_KEY" not in exec_kwargs["env"]
    assert "CODEX_API_KEY" not in exec_kwargs["env"]
    assert "CODEX_ACCESS_TOKEN" not in exec_kwargs["env"]


def test_codex_runtime_blocks_api_key_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", agent_runtime="codex")

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Logged in using an API key\n",
            stderr="",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)
    runtime = CodexAgentRuntime(settings, command_prefix=["codex-test"])

    with pytest.raises(PaidApiAuthenticationBlocked):
        runtime.login_status()


@pytest.mark.asyncio
async def test_codex_runtime_stops_when_included_usage_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", agent_runtime="codex")
    image = tmp_path / "candidate.png"
    image.write_bytes(b"fixture-image")

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Logged in using ChatGPT\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="You've hit your usage limit. Try again later.",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)
    runtime = CodexAgentRuntime(settings, command_prefix=["codex-test"])

    with pytest.raises(AgentUsageLimitReached, match="without API fallback"):
        await runtime.analyze_candidate_image({"candidate_id": 7, "_image_path": str(image)})


def test_runtime_factory_selects_codex_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", agent_runtime="codex")
    monkeypatch.setattr(
        CodexAgentRuntime,
        "resolve_command",
        staticmethod(lambda _settings: ["codex-test"]),
    )

    runtime = runtime_for(settings)

    assert isinstance(runtime, CodexAgentRuntime)
    assert runtime.provider == "codex-chatgpt"
