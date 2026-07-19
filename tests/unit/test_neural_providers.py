from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from runway.config import Settings
from runway.intelligence.embeddings import (
    DeterministicTextEmbeddingProvider,
    RepresentationProviderRegistry,
)
from runway.intelligence.neural_providers import (
    OptionalProviderError,
    SentenceTransformerTextProvider,
    Siglip2EmbeddingProvider,
    configured_provider_registry,
    provider_diagnostics,
)


def test_deterministic_provider_remains_offline_and_functional() -> None:
    result = DeterministicTextEmbeddingProvider().embed_text(
        "Why is Homer so excited?",
        purpose="test",
    )
    assert result.provider == "runway-local"
    assert result.dimensions == 256
    assert result.normalized is True


def test_optional_adapters_require_explicit_local_model_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(OptionalProviderError, match="missing or is not a directory"):
        SentenceTransformerTextProvider(
            missing,
            model_id="local/text",
            revision="revision",
        )
    with pytest.raises(OptionalProviderError, match="missing or is not a directory"):
        Siglip2EmbeddingProvider(
            missing,
            model_id="local/siglip2",
            revision="revision",
        )


def test_configured_registry_never_falls_back_when_model_path_is_missing(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        text_embedding_provider="sentence-transformers",
        text_embedding_model_path=None,
    )
    with pytest.raises(OptionalProviderError, match="MODEL_PATH"):
        configured_provider_registry(settings)
    registry = RepresentationProviderRegistry()
    with pytest.raises(LookupError, match="will not fall back"):
        registry.text("sentence-transformers")


def test_provider_diagnostics_do_not_load_or_download_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    original = importlib.util.find_spec

    def tracked(name: str, *args: object, **kwargs: object) -> object:
        calls.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", tracked)
    settings = Settings(
        data_dir=tmp_path / "data",
        text_embedding_model_path=tmp_path / "not-downloaded",
        multimodal_embedding_model_path=tmp_path / "not-downloaded-either",
    )
    result = provider_diagnostics(settings)
    assert result["automatic_downloads"] is False
    assert result["implicit_activation"] is False
    assert calls == ["sentence_transformers", "torch", "transformers"]
    assert not (tmp_path / "not-downloaded").exists()
    assert not (tmp_path / "not-downloaded-either").exists()
