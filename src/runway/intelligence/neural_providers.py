from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from PIL import Image

from runway.config import Settings
from runway.intelligence.embeddings import (
    RepresentationProviderRegistry,
    RepresentationResult,
    configuration_hash,
)

ProviderKind = Literal["text", "image", "multimodal"]


class OptionalProviderError(RuntimeError):
    """A configured optional provider cannot be loaded safely."""


def _normalise_rows(value: object) -> np.ndarray:
    rows = np.asarray(value, dtype=np.float32)
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    if rows.ndim != 2 or not rows.shape[0] or not rows.shape[1]:
        raise OptionalProviderError("provider returned an empty or invalid representation")
    if not np.all(np.isfinite(rows)):
        raise OptionalProviderError("provider returned nonfinite representation values")
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise OptionalProviderError("provider returned a zero-length representation")
    return cast(np.ndarray, rows / norms)


def _tensor_to_numpy(value: object) -> np.ndarray:
    current: Any = value
    for method in ("detach", "cpu", "float"):
        operation = getattr(current, method, None)
        if callable(operation):
            current = operation()
    numpy_method = getattr(current, "numpy", None)
    if callable(numpy_method):
        current = numpy_method()
    return np.asarray(current, dtype=np.float32)


def _local_model_manifest(path: Path) -> str:
    """Hash small identity files and weight metadata without reading multi-GB weights."""

    rows: list[dict[str, object]] = []
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(path).as_posix()
        stat = candidate.stat()
        row: dict[str, object] = {
            "path": relative,
            "size": stat.st_size,
        }
        if candidate.suffix.casefold() in {".json", ".txt", ".model"} and stat.st_size <= 8_000_000:
            row["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        rows.append(row)
    if not rows:
        raise OptionalProviderError(f"local model directory is empty: {path}")
    return configuration_hash(rows)


def _require_local_model(path: Path | None, *, provider: str) -> tuple[Path, str]:
    if path is None:
        raise OptionalProviderError(
            f"{provider} requires an explicit local model path; automatic downloads are disabled"
        )
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise OptionalProviderError(
            f"{provider} local model path is missing or is not a directory: {resolved}"
        )
    return resolved, _local_model_manifest(resolved)


class SentenceTransformerTextProvider:
    """Offline-only adapter for a locally provisioned Sentence Transformers model."""

    name = "sentence-transformers"

    def __init__(
        self,
        model_path: Path,
        *,
        model_id: str,
        revision: str,
        device: Literal["cpu", "cuda"] = "cpu",
        max_sequence_length: int | None = None,
    ):
        self.model_path, manifest = _require_local_model(
            model_path,
            provider=self.name,
        )
        if not model_id.strip() or not revision.strip():
            raise ValueError("model_id and revision must be explicit")
        if importlib.util.find_spec("sentence_transformers") is None:
            raise OptionalProviderError(
                "sentence-transformers is not installed; install Runway's "
                "`intelligence-ml` optional dependency"
            )
        self.model = model_id.strip()
        self.version = revision.strip()
        self.device = device
        self.max_sequence_length = max_sequence_length
        self.configuration_fingerprint = configuration_hash(
            {
                "adapter": "sentence-transformers-offline-v1",
                "model_id": self.model,
                "revision": self.version,
                "local_path": str(self.model_path),
                "local_manifest": manifest,
                "device": device,
                "max_sequence_length": max_sequence_length,
                "normalization": "l2",
                "local_files_only": True,
                "trust_remote_code": False,
            }
        )
        self._loaded: Any | None = None

    def _load(self) -> Any:
        if self._loaded is None:
            try:
                module = importlib.import_module("sentence_transformers")
                sentence_transformer = module.SentenceTransformer
            except ImportError as exc:  # pragma: no cover - guarded above
                raise OptionalProviderError(
                    "sentence-transformers became unavailable while loading"
                ) from exc
            try:
                loaded = sentence_transformer(
                    str(self.model_path),
                    device=self.device,
                    local_files_only=True,
                    trust_remote_code=False,
                )
            except Exception as exc:
                raise OptionalProviderError(
                    f"failed to load the local Sentence Transformers model: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if self.max_sequence_length is not None:
                loaded.max_seq_length = self.max_sequence_length
            self._loaded = loaded
        return self._loaded

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        if not text.strip():
            raise ValueError("trained text provider cannot embed blank text")
        try:
            encoded = self._load().encode(
                [text],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            if isinstance(exc, OptionalProviderError):
                raise
            raise OptionalProviderError(
                f"trained text embedding failed: {type(exc).__name__}: {exc}"
            ) from exc
        rows = _normalise_rows(encoded)
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=tuple(tuple(float(item) for item in row) for row in rows),
            normalized=True,
            configuration_hash=self.configuration_fingerprint,
        )


class Siglip2EmbeddingProvider:
    """Offline-only image/text-aligned adapter for a local SigLIP 2 checkpoint."""

    name = "siglip2"

    def __init__(
        self,
        model_path: Path,
        *,
        model_id: str,
        revision: str,
        device: Literal["cpu", "cuda"] = "cpu",
        image_weight: float = 0.5,
        text_weight: float = 0.5,
    ):
        self.model_path, manifest = _require_local_model(
            model_path,
            provider=self.name,
        )
        if (
            importlib.util.find_spec("torch") is None
            or importlib.util.find_spec("transformers") is None
        ):
            raise OptionalProviderError(
                "torch and transformers are required for SigLIP 2; install "
                "Runway's `intelligence-ml` optional dependency"
            )
        if not model_id.strip() or not revision.strip():
            raise ValueError("model_id and revision must be explicit")
        if image_weight <= 0 or text_weight <= 0:
            raise ValueError("multimodal weights must be positive")
        self.model = model_id.strip()
        self.version = revision.strip()
        self.device = device
        self.image_weight = image_weight
        self.text_weight = text_weight
        self.configuration_fingerprint = configuration_hash(
            {
                "adapter": "transformers-siglip2-offline-v1",
                "model_id": self.model,
                "revision": self.version,
                "local_path": str(self.model_path),
                "local_manifest": manifest,
                "device": device,
                "normalization": "l2",
                "image_weight": image_weight,
                "text_weight": text_weight,
                "local_files_only": True,
                "trust_remote_code": False,
            }
        )
        self._model: Any | None = None
        self._processor: Any | None = None
        self._torch: Any | None = None

    def _load(self) -> tuple[Any, Any, Any]:
        if self._model is None or self._processor is None or self._torch is None:
            try:
                torch_module = importlib.import_module("torch")
                transformers_module = importlib.import_module("transformers")
                auto_model = transformers_module.AutoModel
                auto_processor = transformers_module.AutoProcessor
            except ImportError as exc:  # pragma: no cover - guarded above
                raise OptionalProviderError(
                    "SigLIP 2 dependencies became unavailable while loading"
                ) from exc
            try:
                processor = auto_processor.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                    trust_remote_code=False,
                )
                model = auto_model.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                    trust_remote_code=False,
                )
                model = model.to(self.device)
                model.eval()
            except Exception as exc:
                raise OptionalProviderError(
                    f"failed to load the local SigLIP 2 model: {type(exc).__name__}: {exc}"
                ) from exc
            self._processor = processor
            self._model = model
            self._torch = torch_module
        return self._model, self._processor, self._torch

    def _features(
        self,
        *,
        path: Path | None = None,
        text: str | None = None,
    ) -> np.ndarray:
        model, processor, torch = self._load()
        if (path is None) == (text is None):
            raise ValueError("exactly one of path or text must be supplied")
        try:
            if path is not None:
                with Image.open(path) as opened:
                    inputs = processor(
                        images=opened.convert("RGB"),
                        return_tensors="pt",
                    )
                method = model.get_image_features
            else:
                if not text or not text.strip():
                    raise ValueError("SigLIP 2 cannot embed blank text")
                inputs = processor(
                    text=[text],
                    padding="max_length",
                    return_tensors="pt",
                )
                method = model.get_text_features
            if hasattr(inputs, "to"):
                inputs = inputs.to(self.device)
            elif isinstance(inputs, dict):
                inputs = {
                    key: value.to(self.device) if hasattr(value, "to") else value
                    for key, value in inputs.items()
                }
            with torch.inference_mode():
                output = method(**inputs)
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise OptionalProviderError(
                f"SigLIP 2 inference failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalise_rows(_tensor_to_numpy(output))

    def embed_image(self, path: Path, *, purpose: str) -> RepresentationResult:
        rows = self._features(path=path)
        return self._result(rows, purpose=purpose)

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        rows = self._features(text=text)
        return self._result(rows, purpose=purpose)

    def embed_image_text(
        self,
        path: Path,
        text: str,
        *,
        purpose: str,
    ) -> RepresentationResult:
        image = self._features(path=path)[0]
        if not text.strip():
            return self._result(image.reshape(1, -1), purpose=purpose)
        text_row = self._features(text=text)[0]
        rows = _normalise_rows(self.image_weight * image + self.text_weight * text_row)
        return self._result(rows, purpose=purpose)

    def _result(self, rows: np.ndarray, *, purpose: str) -> RepresentationResult:
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=tuple(tuple(float(item) for item in row) for row in rows),
            normalized=True,
            configuration_hash=self.configuration_fingerprint,
        )


def configured_provider_registry(settings: Settings) -> RepresentationProviderRegistry:
    """Register only explicitly configured local adapters; never download or activate."""

    registry = RepresentationProviderRegistry()
    if settings.text_embedding_provider == "sentence-transformers":
        if settings.text_embedding_model_path is None:
            raise OptionalProviderError(
                "sentence-transformers is configured but RUNWAY_TEXT_EMBEDDING_MODEL_PATH "
                "is missing"
            )
        text = SentenceTransformerTextProvider(
            settings.text_embedding_model_path,
            model_id=settings.text_embedding_model_id,
            revision=settings.text_embedding_model_revision,
            device=settings.embedding_device,
        )
        registry.register_text(text.name, text)
    if settings.multimodal_embedding_provider == "siglip2":
        if settings.multimodal_embedding_model_path is None:
            raise OptionalProviderError(
                "siglip2 is configured but RUNWAY_MULTIMODAL_EMBEDDING_MODEL_PATH is missing"
            )
        siglip = Siglip2EmbeddingProvider(
            settings.multimodal_embedding_model_path,
            model_id=settings.multimodal_embedding_model_id,
            revision=settings.multimodal_embedding_model_revision,
            device=settings.embedding_device,
        )
        registry.register_image(siglip.name, siglip)
        registry.register_text(siglip.name, siglip)
        registry.register_multimodal(siglip.name, siglip)
    return registry


def provider_diagnostics(settings: Settings) -> dict[str, object]:
    """Report readiness without importing a model or touching network state."""

    dependency_state = {
        package: importlib.util.find_spec(package) is not None
        for package in ("sentence_transformers", "torch", "transformers")
    }

    def local_state(path: Path | None) -> dict[str, object]:
        resolved = path.expanduser().resolve() if path is not None else None
        return {
            "configured": path is not None,
            "path": str(resolved) if resolved is not None else None,
            "exists": bool(resolved and resolved.is_dir()),
        }

    text_path = local_state(settings.text_embedding_model_path)
    multimodal_path = local_state(settings.multimodal_embedding_model_path)
    return {
        "automatic_downloads": False,
        "implicit_activation": False,
        "default_provider": "runway-local",
        "dependencies": dependency_state,
        "providers": [
            {
                "name": "runway-local",
                "tier": 0,
                "kinds": ["text", "image", "multimodal", "multi_vector"],
                "configured": True,
                "load_status": "ready",
                "validated_for_activation": True,
                "trained_model": False,
            },
            {
                "name": "sentence-transformers",
                "tier": 1,
                "kinds": ["text"],
                "configured": settings.text_embedding_provider == "sentence-transformers",
                "model_id": settings.text_embedding_model_id,
                "revision": settings.text_embedding_model_revision,
                "local_model": text_path,
                "dependencies_ready": dependency_state["sentence_transformers"],
                "load_status": (
                    "ready_to_load"
                    if dependency_state["sentence_transformers"] and text_path["exists"]
                    else "unavailable"
                ),
                "validated_for_activation": False,
            },
            {
                "name": "siglip2",
                "tier": 2 if settings.embedding_device == "cuda" else 1,
                "kinds": ["text", "image", "multimodal"],
                "configured": settings.multimodal_embedding_provider == "siglip2",
                "model_id": settings.multimodal_embedding_model_id,
                "revision": settings.multimodal_embedding_model_revision,
                "local_model": multimodal_path,
                "dependencies_ready": dependency_state["torch"]
                and dependency_state["transformers"],
                "load_status": (
                    "ready_to_load"
                    if dependency_state["torch"]
                    and dependency_state["transformers"]
                    and multimodal_path["exists"]
                    else "unavailable"
                ),
                "validated_for_activation": False,
            },
        ],
    }


def provider_status_json(settings: Settings) -> str:
    return json.dumps(provider_diagnostics(settings), indent=2, sort_keys=True)
