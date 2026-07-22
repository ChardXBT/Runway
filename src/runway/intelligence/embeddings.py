from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

import numpy as np
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from runway.db.base import Database
from runway.db.models import (
    RepresentationRecord,
    RepresentationSet,
    RepresentationSetItem,
)
from runway.media.service import inspect_image

TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)
ProviderT = TypeVar("ProviderT")
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
    configuration_fingerprint: str

    def embed_image(self, path: Path, *, purpose: str) -> RepresentationResult: ...


class TextEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str
    configuration_fingerprint: str

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult: ...


class MultimodalEmbeddingProvider(Protocol):
    name: str
    model: str
    version: str
    configuration_fingerprint: str

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
    configuration_fingerprint: str

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
    version = "2"

    def __init__(self, dimensions: int = 256):
        if dimensions < 64:
            raise ValueError("text representation dimensions must be at least 64")
        self.dimensions = dimensions
        self._configuration = {
            "dimensions": dimensions,
            "features": ["words", "bigrams", "character_trigrams", "concept_groups"],
            "unicode": "NFKC-casefold",
            "tokenless_fallback": "normalized-raw-blake2b-v1",
        }
        self.configuration_fingerprint = configuration_hash(self._configuration)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        return TOKEN_RE.findall(normalized)

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        tokens = self._tokens(text)
        features: list[tuple[str, float]] = [(f"w:{token}", 1.0) for token in tokens]
        features.extend(
            (f"b:{tokens[index]}_{tokens[index + 1]}", 0.8) for index in range(len(tokens) - 1)
        )
        compact = " ".join(tokens)
        features.extend(
            (f"c:{compact[index : index + 3]}", 0.22)
            for index in range(max(0, len(compact) - 2))
            if " " not in compact[index : index + 3]
        )
        token_set = set(tokens)
        for index, group in enumerate(CONCEPT_GROUPS):
            if token_set & group:
                features.append((f"concept:{index}", 1.35))
        if not features:
            normalized_raw = unicodedata.normalize("NFKC", text).casefold().strip()
            features.append((f"raw:{normalized_raw or '<empty>'}", 1.0))

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
            configuration_hash=self.configuration_fingerprint,
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
    configuration_fingerprint = configuration_hash(_configuration)

    def embed_image(self, path: Path, *, purpose: str) -> RepresentationResult:
        values = _normalize(np.asarray(inspect_image(path).embedding, dtype=np.float32))
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=(tuple(float(value) for value in values),),
            normalized=True,
            configuration_hash=self.configuration_fingerprint,
        )


class DeterministicMultimodalEmbeddingProvider:
    """Concatenate explicit image and text baselines; not a trained alignment model."""

    name = "runway-local"
    model = "image-text-concatenation"
    version = "2"

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
        self.configuration_fingerprint = configuration_hash(
            {
                "image": {
                    "provider": image_provider.name,
                    "model": image_provider.model,
                    "version": image_provider.version,
                },
                "text": {
                    "provider": text_provider.name,
                    "model": text_provider.model,
                    "version": text_provider.version,
                },
                "image_weight": image_weight,
                "text_weight": text_weight,
            }
        )

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
            np.concatenate([self.image_weight * image, self.text_weight * text_vector]).astype(
                np.float32
            )
        )
        return RepresentationResult(
            provider=self.name,
            model=self.model,
            version=self.version,
            purpose=purpose,
            vectors=(tuple(float(value) for value in vector),),
            normalized=True,
            configuration_hash=self.configuration_fingerprint,
        )


class DeterministicMultiVectorProvider:
    name = "runway-local"
    model = "text-parts"
    version = "1"

    def __init__(self, text_provider: TextEmbeddingProvider):
        self.text_provider = text_provider
        self.configuration_fingerprint = configuration_hash(
            {
                "text_provider": text_provider.name,
                "text_model": text_provider.model,
                "text_version": text_provider.version,
                "pooling": "none",
            }
        )

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
            configuration_hash=self.configuration_fingerprint,
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

    @staticmethod
    def _register(
        providers: dict[str, ProviderT],
        name: str,
        provider: ProviderT,
        *,
        replace: bool,
    ) -> None:
        if not name.strip():
            raise ValueError("provider name cannot be empty")
        if name in providers and not replace:
            raise ValueError(f"representation provider {name!r} is already registered")
        providers[name] = provider

    def register_text(
        self,
        name: str,
        provider: TextEmbeddingProvider,
        *,
        replace: bool = False,
    ) -> None:
        self._register(self._text, name, provider, replace=replace)

    def register_image(
        self,
        name: str,
        provider: ImageEmbeddingProvider,
        *,
        replace: bool = False,
    ) -> None:
        self._register(self._image, name, provider, replace=replace)

    def register_multimodal(
        self,
        name: str,
        provider: MultimodalEmbeddingProvider,
        *,
        replace: bool = False,
    ) -> None:
        self._register(self._multimodal, name, provider, replace=replace)

    def register_multi_vector(
        self,
        name: str,
        provider: MultiVectorEmbeddingProvider,
        *,
        replace: bool = False,
    ) -> None:
        self._register(self._multi_vector, name, provider, replace=replace)

    def text(self, name: str = "runway-local") -> TextEmbeddingProvider:
        try:
            return self._text[name]
        except KeyError as exc:
            raise LookupError(
                f"text representation provider {name!r} is unavailable; "
                "Runway will not fall back to another provider"
            ) from exc

    def image(self, name: str = "runway-local") -> ImageEmbeddingProvider:
        try:
            return self._image[name]
        except KeyError as exc:
            raise LookupError(
                f"image representation provider {name!r} is unavailable; "
                "Runway will not fall back to another provider"
            ) from exc

    def multimodal(self, name: str = "runway-local") -> MultimodalEmbeddingProvider:
        try:
            return self._multimodal[name]
        except KeyError as exc:
            raise LookupError(
                f"multimodal representation provider {name!r} is unavailable; "
                "Runway will not fall back to another provider"
            ) from exc

    def multi_vector(self, name: str = "runway-local") -> MultiVectorEmbeddingProvider:
        try:
            return self._multi_vector[name]
        except KeyError as exc:
            raise LookupError(
                f"multi-vector provider {name!r} is unavailable; "
                "Runway will not fall back to another provider"
            ) from exc

    def status(self) -> dict[str, list[str]]:
        return {
            "text": sorted(self._text),
            "image": sorted(self._image),
            "multimodal": sorted(self._multimodal),
            "multi_vector": sorted(self._multi_vector),
        }


@dataclass(frozen=True)
class RepresentationResolution:
    """Exact provider identity selected for one semantic score path."""

    modality: Literal["text", "image", "multimodal"]
    scope: str
    purpose: str
    provider: str
    model: str
    model_version: str
    configuration_hash: str
    representation_set_id: int | None
    resolution: Literal["active_set", "deterministic_baseline"]

    def as_dict(self) -> dict[str, object]:
        return {
            "modality": self.modality,
            "scope": self.scope,
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "configuration_hash": self.configuration_hash,
            "representation_set_id": self.representation_set_id,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class SemanticSimilarity:
    score: float
    representation: RepresentationResolution


class ActiveRepresentationResolver:
    """Canonical, channel-scoped provider resolver used by all semantic paths.

    Historical records are still resolved by :class:`RepresentationStore`. This
    resolver additionally guarantees that dynamic queries and comparisons use the
    exact provider identity backing the active historical set. If an active set and
    the configured local provider disagree, resolution fails closed instead of
    silently falling back.
    """

    DEFAULTS: dict[str, tuple[str, str]] = {
        "text": ("historical_text", "historical_caption_semantics"),
        "image": ("historical_image", "historical_visual_semantics"),
        "multimodal": ("historical_multimodal", "historical_pair_semantics"),
    }

    def __init__(
        self,
        database: Database,
        registry: RepresentationProviderRegistry | None = None,
    ):
        self.database = database
        if registry is None:
            # Late import avoids a module cycle: neural providers implement the
            # contracts defined in this module.
            from runway.intelligence.neural_providers import configured_provider_registry

            registry = configured_provider_registry(database.settings)
        self.registry = registry
        self.store = RepresentationStore(database)

    def resolve(
        self,
        channel_id: int,
        *,
        modality: Literal["text", "image", "multimodal"],
        scope: str | None = None,
        purpose: str | None = None,
    ) -> tuple[
        TextEmbeddingProvider | ImageEmbeddingProvider | MultimodalEmbeddingProvider,
        RepresentationResolution,
    ]:
        default_scope, default_purpose = self.DEFAULTS[modality]
        effective_scope = scope or default_scope
        effective_purpose = purpose or default_purpose
        active_set = self.store.active_set(
            channel_id=channel_id,
            scope=effective_scope,
            purpose=effective_purpose,
        )
        provider_name = active_set.provider if active_set is not None else "runway-local"
        provider: TextEmbeddingProvider | ImageEmbeddingProvider | MultimodalEmbeddingProvider
        if modality == "text":
            provider = self.registry.text(provider_name)
        elif modality == "image":
            provider = self.registry.image(provider_name)
        else:
            provider = self.registry.multimodal(provider_name)
        if active_set is not None and (
            provider.model != active_set.model
            or provider.version != active_set.model_version
            or provider.configuration_fingerprint != active_set.configuration_hash
        ):
            raise RuntimeError(
                "configured provider identity does not match active representation "
                f"set {active_set.id}; no fallback was attempted"
            )
        return provider, RepresentationResolution(
            modality=modality,
            scope=effective_scope,
            purpose=effective_purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            configuration_hash=provider.configuration_fingerprint,
            representation_set_id=active_set.id if active_set is not None else None,
            resolution=("active_set" if active_set is not None else "deterministic_baseline"),
        )

    def text_similarity(
        self,
        channel_id: int,
        first: str,
        second: str,
        *,
        score_purpose: str,
    ) -> SemanticSimilarity:
        raw_provider, resolution = self.resolve(channel_id, modality="text")
        provider = cast(TextEmbeddingProvider, raw_provider)
        first_vector = provider.embed_text(first, purpose=score_purpose).as_array()[0]
        second_vector = provider.embed_text(second, purpose=score_purpose).as_array()[0]
        return SemanticSimilarity(
            score=cosine(first_vector, second_vector),
            representation=resolution,
        )

    def text_vector(
        self,
        channel_id: int,
        text: str,
        *,
        score_purpose: str,
    ) -> tuple[np.ndarray, RepresentationResolution]:
        raw_provider, resolution = self.resolve(channel_id, modality="text")
        provider = cast(TextEmbeddingProvider, raw_provider)
        return provider.embed_text(text, purpose=score_purpose).as_array()[0], resolution

    def image_vector(
        self,
        channel_id: int,
        path: Path,
        *,
        score_purpose: str,
    ) -> tuple[np.ndarray, RepresentationResolution]:
        raw_provider, resolution = self.resolve(channel_id, modality="image")
        provider = cast(ImageEmbeddingProvider, raw_provider)
        return provider.embed_image(path, purpose=score_purpose).as_array()[0], resolution

    def multimodal_vector(
        self,
        channel_id: int,
        path: Path,
        text: str,
        *,
        score_purpose: str,
    ) -> tuple[np.ndarray, RepresentationResolution]:
        raw_provider, resolution = self.resolve(channel_id, modality="multimodal")
        provider = cast(MultimodalEmbeddingProvider, raw_provider)
        return (
            provider.embed_image_text(path, text, purpose=score_purpose).as_array()[0],
            resolution,
        )

    def snapshot(self, channel_id: int) -> dict[str, object]:
        result: dict[str, object] = {}
        modalities: tuple[
            Literal["text", "image", "multimodal"],
            Literal["text", "image", "multimodal"],
            Literal["text", "image", "multimodal"],
        ] = ("text", "image", "multimodal")
        for modality in modalities:
            _provider, resolution = self.resolve(
                channel_id,
                modality=modality,
            )
            result[modality] = resolution.as_dict()
        return result


class RepresentationStore:
    def __init__(self, database: Database):
        self.database = database
        self._cache_diagnostics: Counter[str] = Counter()

    def reset_diagnostics(self) -> None:
        self._cache_diagnostics.clear()

    def diagnostics(self) -> dict[str, int]:
        return {
            key: int(self._cache_diagnostics.get(key, 0))
            for key in (
                "exact_hits",
                "active_hits",
                "misses",
                "stale_misses",
                "recomputations",
                "active_set_fail_closed",
            )
        }

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
            return self.persist_in_session(
                session,
                channel_id=channel_id,
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                modality=modality,
                result=result,
                source_content_hash=source_content_hash,
                metadata=metadata,
            )

    @staticmethod
    def persist_in_session(
        session: Session,
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
        """Persist an exact representation inside an existing atomic decision."""
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

    def get_exact(
        self,
        *,
        channel_id: int,
        entity_type: str,
        entity_id: int,
        field: str,
        purpose: str,
        provider: str,
        model: str,
        model_version: str,
        source_content_hash: str,
        configuration_hash: str,
    ) -> RepresentationRecord | None:
        with self.database.session() as session:
            record = session.scalar(
                select(RepresentationRecord)
                .where(
                    RepresentationRecord.channel_id == channel_id,
                    RepresentationRecord.entity_type == entity_type,
                    RepresentationRecord.entity_id == entity_id,
                    RepresentationRecord.field == field,
                    RepresentationRecord.purpose == purpose,
                    RepresentationRecord.provider == provider,
                    RepresentationRecord.model == model,
                    RepresentationRecord.model_version == model_version,
                    RepresentationRecord.source_content_hash == source_content_hash,
                    RepresentationRecord.configuration_hash == configuration_hash,
                )
                .limit(1)
            )
        if record is not None:
            self._cache_diagnostics["exact_hits"] += 1
        return record

    def active_set(
        self,
        *,
        channel_id: int,
        scope: str,
        purpose: str,
    ) -> RepresentationSet | None:
        with self.database.session() as session:
            rows = session.scalars(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == channel_id,
                    RepresentationSet.scope == scope,
                    RepresentationSet.purpose == purpose,
                    RepresentationSet.active.is_(True),
                    RepresentationSet.status == "active",
                )
                .order_by(RepresentationSet.id)
            ).all()
        if len(rows) > 1:
            raise RuntimeError(
                "multiple active representation sets violate the canonical resolver "
                f"for channel={channel_id}, scope={scope!r}, purpose={purpose!r}"
            )
        return rows[0] if rows else None

    def get_active(
        self,
        *,
        channel_id: int,
        scope: str,
        purpose: str,
        entity_type: str,
        entity_id: int,
        field: str,
        source_content_hash: str | None = None,
    ) -> RepresentationRecord | None:
        active_set = self.active_set(
            channel_id=channel_id,
            scope=scope,
            purpose=purpose,
        )
        if active_set is None:
            return None
        with self.database.session() as session:
            item = session.scalar(
                select(RepresentationSetItem)
                .where(
                    RepresentationSetItem.representation_set_id == active_set.id,
                    RepresentationSetItem.channel_id == channel_id,
                    RepresentationSetItem.entity_type == entity_type,
                    RepresentationSetItem.entity_id == entity_id,
                    RepresentationSetItem.field == field,
                )
                .limit(1)
            )
            if item is None or item.status != "complete" or item.representation_record_id is None:
                self._cache_diagnostics["active_set_fail_closed"] += 1
                return None
            if source_content_hash is not None and item.source_content_hash != source_content_hash:
                self._cache_diagnostics["stale_misses"] += 1
                return None
            record = session.get(
                RepresentationRecord,
                item.representation_record_id,
            )
            if record is None:
                self._cache_diagnostics["active_set_fail_closed"] += 1
                return None
            if (
                record.channel_id != channel_id
                or record.purpose != purpose
                or record.provider != active_set.provider
                or record.model != active_set.model
                or record.model_version != active_set.model_version
                or record.configuration_hash != active_set.configuration_hash
                or record.source_content_hash != item.source_content_hash
            ):
                self._cache_diagnostics["active_set_fail_closed"] += 1
                return None
        self._cache_diagnostics["active_hits"] += 1
        return record

    def get_or_create(
        self,
        *,
        channel_id: int,
        entity_type: str,
        entity_id: int,
        field: str,
        modality: str,
        purpose: str,
        provider: str,
        model: str,
        model_version: str,
        source_content_hash: str,
        configuration_hash: str,
        producer: Callable[[], RepresentationResult],
        metadata: dict[str, object] | None = None,
        active_scope: str | None = None,
    ) -> RepresentationRecord:
        if active_scope is not None:
            active_set = self.active_set(
                channel_id=channel_id,
                scope=active_scope,
                purpose=purpose,
            )
            if active_set is not None:
                record = self.get_active(
                    channel_id=channel_id,
                    scope=active_scope,
                    purpose=purpose,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field=field,
                    source_content_hash=source_content_hash,
                )
                if record is None:
                    raise LookupError(
                        "the active representation set does not contain a valid exact "
                        f"record for {entity_type}:{entity_id}:{field}"
                    )
                return record

        exact = self.get_exact(
            channel_id=channel_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            purpose=purpose,
            provider=provider,
            model=model,
            model_version=model_version,
            source_content_hash=source_content_hash,
            configuration_hash=configuration_hash,
        )
        if exact is not None:
            return exact

        self._cache_diagnostics["misses"] += 1
        result = producer()
        if (
            result.provider != provider
            or result.model != model
            or result.version != model_version
            or result.purpose != purpose
            or result.configuration_hash != configuration_hash
        ):
            raise ValueError("representation producer returned an unexpected identity")
        self._cache_diagnostics["recomputations"] += 1
        return self.persist(
            channel_id=channel_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            modality=modality,
            result=result,
            source_content_hash=source_content_hash,
            metadata=metadata,
        )

    def records_for_set(self, representation_set_id: int) -> list[RepresentationRecord]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(RepresentationRecord)
                    .join(
                        RepresentationSetItem,
                        RepresentationSetItem.representation_record_id == RepresentationRecord.id,
                    )
                    .where(
                        RepresentationSetItem.representation_set_id == representation_set_id,
                        RepresentationSetItem.status == "complete",
                    )
                    .order_by(RepresentationSetItem.id)
                ).all()
            )

    def coverage(self, representation_set_id: int) -> dict[str, object]:
        with self.database.session() as session:
            representation_set = session.get(RepresentationSet, representation_set_id)
            if representation_set is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            rows = session.execute(
                select(
                    RepresentationSetItem.status,
                    func.count(RepresentationSetItem.id),
                )
                .where(RepresentationSetItem.representation_set_id == representation_set_id)
                .group_by(RepresentationSetItem.status)
            ).all()
        counts = {str(status): int(count) for status, count in rows}
        completed = counts.get("complete", 0)
        expected = representation_set.expected_count
        return {
            "representation_set_id": representation_set_id,
            "status": representation_set.status,
            "active": representation_set.active,
            "expected": expected,
            "complete": completed,
            "failed": counts.get("failed", 0),
            "stale": counts.get("stale", 0),
            "pending": counts.get("pending", 0),
            "coverage": completed / expected if expected else 1.0,
            "counts": counts,
        }

    def mark_stale(
        self,
        *,
        channel_id: int,
        entity_type: str,
        entity_id: int,
        field: str,
        current_source_content_hash: str,
    ) -> int:
        with self.database.session() as session:
            items = session.scalars(
                select(RepresentationSetItem)
                .where(
                    RepresentationSetItem.channel_id == channel_id,
                    RepresentationSetItem.entity_type == entity_type,
                    RepresentationSetItem.entity_id == entity_id,
                    RepresentationSetItem.field == field,
                    RepresentationSetItem.source_content_hash != current_source_content_hash,
                    RepresentationSetItem.status == "complete",
                )
                .order_by(RepresentationSetItem.id)
            ).all()
            touched_sets: set[int] = set()
            for item in items:
                item.status = "stale"
                item.error_summary = "source content hash changed"
                touched_sets.add(item.representation_set_id)
            for set_id in touched_sets:
                representation_set = session.get(RepresentationSet, set_id)
                if representation_set is None:
                    continue
                representation_set.stale_count = int(
                    session.scalar(
                        select(func.count(RepresentationSetItem.id)).where(
                            RepresentationSetItem.representation_set_id == set_id,
                            RepresentationSetItem.status == "stale",
                        )
                    )
                    or 0
                )
                representation_set.status = "stale"
                representation_set.active = False
        return len(items)

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
