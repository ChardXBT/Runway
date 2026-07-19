from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from sqlalchemy import desc, select

from runway.db.base import Database
from runway.db.models import RepresentationRecord
from runway.media.service import inspect_image

TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)
CONCEPT_GROUPS = (
    frozenset({"excited", "eager", "thrilled", "delighted", "enthusiastic"}),
    frozenset({"surprised", "shocked", "astonished", "startled"}),
    frozenset({"angry", "furious", "annoyed", "irritated"}),
    frozenset({"sad", "upset", "unhappy", "dejected"}),
    frozenset({"team", "group", "squad", "crew"}),
    frozenset({"learn", "explain", "teach", "education", "science"}),
    frozenset({"game", "gaming", "player", "match", "level"}),
    frozenset({"product", "brand", "launch", "feature", "release"}),
    frozenset({"why", "reason", "cause"}),
    frozenset({"how", "method", "process"}),
)


def configuration_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


@dataclass(frozen=True)
class RepresentationResult:
    provider: str
    model: str
    version: str
    purpose: str
    vectors: tuple[tuple[float, ...], ...]
    normalized: bool
    configuration_hash: str

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0

    @property
    def vector_count(self) -> int:
        return len(self.vectors)

    def as_array(self) -> np.ndarray:
        return np.asarray(self.vectors, dtype=np.float32)

    def as_bytes(self) -> bytes:
        return self.as_array().tobytes()


class ImageEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str

    def embed_image(self, path: Path, *, purpose: str) -> RepresentationResult: ...


class TextEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult: ...


class MultimodalEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str

    def embed_image_text(
        self,
        path: Path,
        text: str,
        *,
        purpose: str,
    ) -> RepresentationResult: ...


class MultiVectorEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str

    def embed_text_parts(
        self,
        parts: Sequence[str],
        *,
        purpose: str,
    ) -> RepresentationResult: ...


class DeterministicTextEmbeddingProvider:
    """Dependency-light semantic baseline; this is not a trained language model."""

    name = "runway-local"
    model = "signed-subword-concepts"
    version = "1"

    def __init__(self, dimensions: int = 256):
        if dimensions < 64:
            raise ValueError("text representation dimensions must be at least 64")
        self.dimensions = dimensions
        self._configuration = {
            "dimensions": dimensions,
            "features": ["words", "bigrams", "character_trigrams", "concept_groups"],
            "unicode": "NFKC-casefold",
        }

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        return TOKEN_RE.findall(normalized)

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        tokens = self._tokens(text)
        features: list[tuple[str, float]] = [(f"w:{token}", 1.0) for token in tokens]
        features.extend(
            (f"b:{tokens[index]}_{tokens[index + 1]}", 0.8)
            for index in range(len(tokens) - 1)
        )
        compact = " ".join(tokens)
        features.extend(
            (f"c:{compact[index:index + 3]}", 0.22)
            for index in range(max(0, len(compact) - 2))
            if " " not in compact[index : index + 3]
        )
        token_set = set(tokens)
        for index, group in enumerate(CONCEPT_GROUPS):
            if token_set & group:
                features.append((f"concept:{index}", 1.35))

        vector = np.zeros(self.dimensions, dtype=np.float32)
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[bucket] += sign * weight
        vector = _normalize(vector)
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=(tuple(float(value) for value in vector),),
            normalized=True,
            configuration_hash=configuration_hash(self._configuration),
        )


class DeterministicImageEmbeddingProvider:
    """Wrap the existing local descriptor in the versioned provider contract."""

    name = "runway-local"
    model = "image-descriptor"
    version = "3"
    _configuration = {
        "source": "runway-local-image-v3",
        "trained_semantics": False,
        "normalization": "l2",
    }

    def embed_image(self, path: Path, *, purpose: str) -> RepresentationResult:
        values = _normalize(np.asarray(inspect_image(path).embedding, dtype=np.float32))
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=(tuple(float(value) for value in values),),
            normalized=True,
            configuration_hash=configuration_hash(self._configuration),
        )


class DeterministicMultimodalEmbeddingProvider:
    """Concatenate explicit image and text baselines; not a trained alignment model."""

    name = "runway-local"
    model = "image-text-concatenation"
    version = "1"

    def __init__(
        self,
        image_provider: ImageEmbeddingProvider,
        text_provider: TextEmbeddingProvider,
        *,
        image_weight: float = 0.55,
        text_weight: float = 0.45,
    ):
        if image_weight <= 0 or text_weight <= 0:
            raise ValueError("multimodal weights must be positive")
        self.image_provider = image_provider
        self.text_provider = text_provider
        self.image_weight = image_weight
        self.text_weight = text_weight

    def embed_image_text(
        self,
        path: Path,
        text: str,
        *,
        purpose: str,
    ) -> RepresentationResult:
        image = self.image_provider.embed_image(path, purpose=purpose).as_array()[0]
        text_vector = self.text_provider.embed_text(text, purpose=purpose).as_array()[0]
        vector = _normalize(
            np.concatenate(
                [self.image_weight * image, self.text_weight * text_vector]
            ).astype(np.float32)
        )
        config = {
            "image": {
                "provider": self.image_provider.name,
                "model": self.image_provider.model,
                "version": self.image_provider.version,
            },
            "text": {
                "provider": self.text_provider.name,
                "model": self.text_provider.model,
                "version": self.text_provider.version,
            },
            "image_weight": self.image_weight,
            "text_weight": self.text_weight,
        }
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=(tuple(float(value) for value in vector),),
            normalized=True,
            configuration_hash=configuration_hash(config),
        )


class DeterministicMultiVectorProvider:
    name = "runway-local"
    model = "text-parts"
    version = "1"

    def __init__(self, text_provider: TextEmbeddingProvider):
        self.text_provider = text_provider

    def embed_text_parts(
        self,
        parts: Sequence[str],
        *,
        purpose: str,
    ) -> RepresentationResult:
        rows = tuple(
            tuple(
                float(value)
                for value in self.text_provider.embed_text(
                    part,
                    purpose=purpose,
                ).as_array()[0]
            )
            for part in parts
        )
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=rows,
            normalized=True,
            configuration_hash=configuration_hash(
                {
                    "text_provider": self.text_provider.name,
                    "text_model": self.text_provider.model,
                    "text_version": self.text_provider.version,
                    "pooling": "none",
                }
            ),
        )


class RepresentationProviderRegistry:
    def __init__(self) -> None:
        text = DeterministicTextEmbeddingProvider()
        image = DeterministicImageEmbeddingProvider()
        self._text: dict[str, TextEmbeddingProvider] = {"runway-local": text}
        self._image: dict[str, ImageEmbeddingProvider] = {"runway-local": image}
        self._multimodal: dict[str, MultimodalEmbeddingProvider] = {
            "runway-local": DeterministicMultimodalEmbeddingProvider(image, text)
        }
        self._multi_vector: dict[str, MultiVectorEmbeddingProvider] = {
            "runway-local": DeterministicMultiVectorProvider(text)
        }

    def text(self, name: str = "runway-local") -> TextEmbeddingProvider:
        try:
            return self._text[name]
        except KeyError as exc:
            raise LookupError(
                f"text representation provider {name!r} is unavailable; "
                "RunWay will not fall back to another provider"
            ) from exc

    def image(self, name: str = "runway-local") -> ImageEmbeddingProvider:
        try:
            return self._image[name]
        except KeyError as exc:
            raise LookupError(
                f"image representation provider {name!r} is unavailable; "
                "RunWay will not fall back to another provider"
            ) from exc

    def multimodal(self, name: str = "runway-local") -> MultimodalEmbeddingProvider:
        try:
            return self._multimodal[name]
        except KeyError as exc:
            raise LookupError(
                f"multimodal representation provider {name!r} is unavailable; "
                "RunWay will not fall back to another provider"
            ) from exc

    def multi_vector(self, name: str = "runway-local") -> MultiVectorEmbeddingProvider:
        try:
            return self._multi_vector[name]
        except KeyError as exc:
            raise LookupError(
                f"multi-vector provider {name!r} is unavailable; "
                "RunWay will not fall back to another provider"
            ) from exc

    def status(self) -> dict[str, list[str]]:
        return {
            "text": sorted(self._text),
            "image": sorted(self._image),
            "multimodal": sorted(self._multimodal),
            "multi_vector": sorted(self._multi_vector),
        }


class RepresentationStore:
    def __init__(self, database: Database):
        self.database = database

    def persist(
        self,
        *,
        channel_id: int,
        entity_type: str,
        entity_id: int,
        field: str,
        modality: str,
        result: RepresentationResult,
        source_content_hash: str,
        metadata: dict[str, object] | None = None,
    ) -> RepresentationRecord:
        with self.database.session() as session:
            identity = (
                RepresentationRecord.channel_id == channel_id,
                RepresentationRecord.entity_type == entity_type,
                RepresentationRecord.entity_id == entity_id,
                RepresentationRecord.field == field,
                RepresentationRecord.purpose == result.purpose,
                RepresentationRecord.provider == result.provider,
                RepresentationRecord.model == result.model,
                RepresentationRecord.model_version == result.version,
                RepresentationRecord.source_content_hash == source_content_hash,
                RepresentationRecord.configuration_hash == result.configuration_hash,
            )
            existing = session.scalar(select(RepresentationRecord).where(*identity).limit(1))
            if existing is not None:
                return existing
            record = RepresentationRecord(
                channel_id=channel_id,
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                modality=modality,
                purpose=result.purpose,
                provider=result.provider,
                model=result.model,
                model_version=result.version,
                dimensions=result.dimensions,
                vector_count=result.vector_count,
                dtype="float32",
                serialized_data=result.as_bytes(),
                normalized=result.normalized,
                source_content_hash=source_content_hash,
                configuration_hash=result.configuration_hash,
                metadata_json=json.dumps(metadata or {}, sort_keys=True),
                active=True,
            )
            session.add(record)
            session.flush()
            return record

    def latest(
        self,
        *,
        channel_id: int,
        entity_type: str,
        entity_id: int,
        purpose: str,
    ) -> RepresentationRecord | None:
        with self.database.session() as session:
            return session.scalar(
                select(RepresentationRecord)
                .where(
                    RepresentationRecord.channel_id == channel_id,
                    RepresentationRecord.entity_type == entity_type,
                    RepresentationRecord.entity_id == entity_id,
                    RepresentationRecord.purpose == purpose,
                    RepresentationRecord.active.is_(True),
                )
                .order_by(desc(RepresentationRecord.created_at), desc(RepresentationRecord.id))
                .limit(1)
            )

    @staticmethod
    def vectors(record: RepresentationRecord) -> np.ndarray:
        values = np.frombuffer(record.serialized_data, dtype=np.float32)
        if record.dimensions <= 0 or values.size != record.dimensions * record.vector_count:
            raise ValueError(f"representation {record.id} has inconsistent dimensions")
        return values.reshape(record.vector_count, record.dimensions)


def mean_pool(results: Iterable[RepresentationResult], *, purpose: str) -> RepresentationResult:
    rows = [result.as_array()[0] for result in results if result.vector_count]
    if not rows:
        raise ValueError("cannot pool an empty representation set")
    dimensions = {row.size for row in rows}
    if len(dimensions) != 1:
        raise ValueError("pooled representations must have matching dimensions")
    vector = _normalize(np.mean(np.vstack(rows), axis=0))
    return RepresentationResult(
        provider="runway-local",
        model="mean-pool",
        version="1",
        purpose=purpose,
        vectors=(tuple(float(value) for value in vector),),
        normalized=True,
        configuration_hash=configuration_hash({"pooling": "mean", "count": len(rows)}),
    )


def cosine(first: np.ndarray, second: np.ndarray) -> float:
    if first.shape != second.shape or not first.size:
        return 0.0
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if not denominator:
        return 0.0
    return float(np.dot(first, second) / denominator)
