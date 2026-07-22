from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from runway.config import Settings
from runway.intelligence.embeddings import (
    DeterministicImageEmbeddingProvider,
    DeterministicMultimodalEmbeddingProvider,
    DeterministicTextEmbeddingProvider,
    TextEmbeddingProvider,
    cosine,
)
from runway.intelligence.neural_providers import (
    OptionalProviderError,
    SentenceTransformerTextProvider,
    Siglip2EmbeddingProvider,
)


class NeuralChallenger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    modality: Literal["text", "image_text", "unified_multimodal"]
    adapter: str
    model_id: str
    practical_tier: Literal["small", "medium", "research"]
    instruction_aware: bool = False
    query_prefix: str = ""
    document_prefix: str = ""
    adapter_available: bool = True
    license_gate: str = "verify_local_model_card_before_commercial_activation"


CHALLENGERS: tuple[NeuralChallenger, ...] = (
    NeuralChallenger(
        key="qwen3-embedding-0.6b",
        modality="text",
        adapter="sentence-transformers",
        model_id="Qwen/Qwen3-Embedding-0.6B",
        practical_tier="medium",
        instruction_aware=True,
        query_prefix="Instruct: Retrieve creator-history examples relevant to the query.\nQuery: ",
    ),
    NeuralChallenger(
        key="bge-m3",
        modality="text",
        adapter="sentence-transformers",
        model_id="BAAI/bge-m3",
        practical_tier="medium",
        instruction_aware=True,
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    NeuralChallenger(
        key="all-minilm-l6-v2",
        modality="text",
        adapter="sentence-transformers",
        model_id="sentence-transformers/all-MiniLM-L6-v2",
        practical_tier="small",
    ),
    NeuralChallenger(
        key="siglip2-base-patch16-224",
        modality="image_text",
        adapter="siglip2",
        model_id="google/siglip2-base-patch16-224",
        practical_tier="medium",
    ),
    NeuralChallenger(
        key="gme-research",
        modality="unified_multimodal",
        adapter="unimplemented_research_boundary",
        model_id="GME",
        practical_tier="research",
        adapter_available=False,
    ),
    NeuralChallenger(
        key="vlm2vec-v2-research",
        modality="unified_multimodal",
        adapter="unimplemented_research_boundary",
        model_id="VLM2Vec-V2",
        practical_tier="research",
        adapter_available=False,
    ),
)


class TextRetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    query: str
    relevant_documents: list[str] = Field(min_length=1)
    distractor_documents: list[str] = Field(min_length=1)
    label_source: Literal["human", "teacher", "synthetic", "policy"]
    group_key: str


class ImageTextRetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    image_path: Path
    relevant_captions: list[str] = Field(min_length=1)
    distractor_captions: list[str] = Field(min_length=1)
    label_source: Literal["human", "teacher", "synthetic", "policy"]
    group_key: str


class NeuralChallengerEvaluator:
    """Offline-only evaluation; it never downloads or activates a model."""

    activation_minimum_human_cases = 50

    def __init__(self, settings: Settings):
        self.settings = settings

    def readiness(self, model_paths: dict[str, Path] | None = None) -> dict[str, object]:
        paths = model_paths or {}
        rows: list[dict[str, object]] = []
        for challenger in CHALLENGERS:
            path = paths.get(challenger.key)
            rows.append(
                {
                    **challenger.model_dump(),
                    "local_path": str(path.resolve()) if path is not None else None,
                    "local_path_exists": bool(path and path.resolve().is_dir()),
                    "status": (
                        "adapter_unavailable"
                        if not challenger.adapter_available
                        else "ready_for_offline_evaluation"
                        if path is not None and path.resolve().is_dir()
                        else "blocked_missing_local_weights"
                    ),
                    "automatic_downloads": False,
                    "implicit_activation": False,
                }
            )
        return {
            "catalog_version": "runway-neural-challengers-v1",
            "challengers": rows,
            "automatic_downloads": False,
            "implicit_activation": False,
        }

    def evaluate_text(
        self,
        challenger_key: str,
        *,
        model_path: Path,
        revision: str,
        cases: list[TextRetrievalCase],
        license_verified: bool = False,
    ) -> dict[str, object]:
        challenger = self._challenger(challenger_key, modality="text")
        if not cases:
            raise ValueError("text challenger evaluation requires labelled cases")
        provider = SentenceTransformerTextProvider(
            model_path,
            model_id=challenger.model_id,
            revision=revision,
            device=self.settings.embedding_device,
            query_prefix=challenger.query_prefix,
            document_prefix=challenger.document_prefix,
        )
        baseline = DeterministicTextEmbeddingProvider()
        candidate_metrics = self._text_metrics(provider, cases)
        baseline_metrics = self._text_metrics(baseline, cases)
        human_cases = sum(case.label_source == "human" for case in cases)
        retrieval_gate_passed = (
            human_cases >= self.activation_minimum_human_cases
            and candidate_metrics["mrr"] > baseline_metrics["mrr"]
            and candidate_metrics["recall_at_3"] >= baseline_metrics["recall_at_3"]
        )
        blocked_reasons = [
            reason
            for condition, reason in (
                (
                    human_cases < self.activation_minimum_human_cases,
                    f"requires at least {self.activation_minimum_human_cases} human cases",
                ),
                (
                    candidate_metrics["mrr"] <= baseline_metrics["mrr"],
                    "candidate did not improve MRR",
                ),
                (
                    candidate_metrics["recall_at_3"] < baseline_metrics["recall_at_3"],
                    "candidate regressed Recall@3",
                ),
                (not license_verified, "commercial licence review is not recorded"),
            )
            if condition
        ]
        blocked_reasons.extend(
            [
                "downstream blind caption-preference gate pending",
                "duplicate and grounding non-regression gates pending",
                "channel-isolation evaluation pending",
                "100 percent representation backfill pending",
                "rollback rehearsal pending",
            ]
        )
        return {
            "evaluation_version": "runway-text-retrieval-challenger-v1",
            "challenger": challenger.model_dump(),
            "revision": revision,
            "local_path": str(model_path.resolve()),
            "case_count": len(cases),
            "human_case_count": human_cases,
            "label_sources": sorted({case.label_source for case in cases}),
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "delta": {
                "mrr": round(candidate_metrics["mrr"] - baseline_metrics["mrr"], 6),
                "recall_at_3": round(
                    candidate_metrics["recall_at_3"] - baseline_metrics["recall_at_3"],
                    6,
                ),
            },
            "offline_retrieval_gate_passed": retrieval_gate_passed,
            "activation_eligible": False,
            "activation_performed": False,
            "blocked_reasons": blocked_reasons,
            "hardware": self._hardware(model_path),
            "license_verified": license_verified,
            "automatic_downloads": False,
        }

    def evaluate_image_text(
        self,
        challenger_key: str,
        *,
        model_path: Path,
        revision: str,
        cases: list[ImageTextRetrievalCase],
        license_verified: bool = False,
    ) -> dict[str, object]:
        challenger = self._challenger(challenger_key, modality="image_text")
        if not cases:
            raise ValueError("image-text evaluation requires labelled cases")
        provider = Siglip2EmbeddingProvider(
            model_path,
            model_id=challenger.model_id,
            revision=revision,
            device=self.settings.embedding_device,
        )
        started = time.perf_counter()
        reciprocal_ranks: list[float] = []
        recall_at_3 = 0
        baseline_text = DeterministicTextEmbeddingProvider()
        baseline = DeterministicMultimodalEmbeddingProvider(
            DeterministicImageEmbeddingProvider(),
            baseline_text,
        )
        baseline_reciprocal_ranks: list[float] = []
        baseline_recall_at_3 = 0
        for case in cases:
            if not case.image_path.is_file():
                raise FileNotFoundError(case.image_path)
            image = provider.embed_image(
                case.image_path,
                purpose="evaluation_image_query",
            ).as_array()[0]
            documents = [*case.relevant_captions, *case.distractor_captions]
            relevant = set(range(len(case.relevant_captions)))
            ranked = sorted(
                range(len(documents)),
                key=lambda index: (
                    -cosine(
                        image,
                        provider.embed_text(
                            documents[index],
                            purpose="evaluation_caption_document",
                        ).as_array()[0],
                    ),
                    index,
                ),
            )
            first_relevant_rank = min(ranked.index(index) + 1 for index in relevant)
            reciprocal_ranks.append(1.0 / first_relevant_rank)
            recall_at_3 += bool(relevant & set(ranked[:3]))
            baseline_query = baseline.embed_image_text(
                case.image_path,
                "",
                purpose="evaluation_image_query_baseline",
            ).as_array()[0]
            baseline_ranked = sorted(
                range(len(documents)),
                key=lambda index: (
                    -cosine(
                        baseline_query,
                        baseline.embed_image_text(
                            case.image_path,
                            documents[index],
                            purpose="evaluation_caption_document_baseline",
                        ).as_array()[0],
                    ),
                    index,
                ),
            )
            first_baseline_rank = min(baseline_ranked.index(index) + 1 for index in relevant)
            baseline_reciprocal_ranks.append(1.0 / first_baseline_rank)
            baseline_recall_at_3 += bool(relevant & set(baseline_ranked[:3]))
        human_cases = sum(case.label_source == "human" for case in cases)
        elapsed = (time.perf_counter() - started) * 1000
        candidate_metrics = {
            "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6),
            "recall_at_3": round(recall_at_3 / len(cases), 6),
            "latency_ms": round(elapsed, 3),
        }
        baseline_metrics = {
            "mrr": round(
                sum(baseline_reciprocal_ranks) / len(baseline_reciprocal_ranks),
                6,
            ),
            "recall_at_3": round(baseline_recall_at_3 / len(cases), 6),
        }
        retrieval_gate_passed = (
            human_cases >= self.activation_minimum_human_cases
            and candidate_metrics["mrr"] > baseline_metrics["mrr"]
            and candidate_metrics["recall_at_3"] >= baseline_metrics["recall_at_3"]
        )
        return {
            "evaluation_version": "runway-image-text-challenger-v1",
            "challenger": challenger.model_dump(),
            "revision": revision,
            "local_path": str(model_path.resolve()),
            "case_count": len(cases),
            "human_case_count": human_cases,
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "delta": {
                "mrr": round(candidate_metrics["mrr"] - baseline_metrics["mrr"], 6),
                "recall_at_3": round(
                    candidate_metrics["recall_at_3"] - baseline_metrics["recall_at_3"],
                    6,
                ),
            },
            "offline_retrieval_gate_passed": retrieval_gate_passed,
            "activation_eligible": False,
            "activation_performed": False,
            "blocked_reasons": [
                *(
                    [f"requires at least {self.activation_minimum_human_cases} human cases"]
                    if human_cases < self.activation_minimum_human_cases
                    else []
                ),
                *(
                    ["candidate did not improve deterministic baseline MRR"]
                    if candidate_metrics["mrr"] <= baseline_metrics["mrr"]
                    else []
                ),
                *(["commercial licence review is not recorded"] if not license_verified else []),
                "duplicate, grounding, channel-isolation, hardware, and rollback gates pending",
            ],
            "hardware": self._hardware(model_path),
            "license_verified": license_verified,
            "automatic_downloads": False,
        }

    @staticmethod
    def write_report(report: dict[str, object], destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return destination

    @staticmethod
    def load_text_cases(path: Path) -> list[TextRetrievalCase]:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("text evaluation dataset must be a JSON array")
        return [TextRetrievalCase.model_validate(row) for row in payload]

    @staticmethod
    def load_image_text_cases(path: Path) -> list[ImageTextRetrievalCase]:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("image-text evaluation dataset must be a JSON array")
        return [ImageTextRetrievalCase.model_validate(row) for row in payload]

    @staticmethod
    def _text_metrics(
        provider: TextEmbeddingProvider,
        cases: list[TextRetrievalCase],
    ) -> dict[str, float]:
        started = time.perf_counter()
        reciprocal_ranks: list[float] = []
        recall_at_1 = 0
        recall_at_3 = 0
        for case in cases:
            query = provider.embed_text(
                case.query,
                purpose="evaluation_query",
            ).as_array()[0]
            documents = [*case.relevant_documents, *case.distractor_documents]
            relevant = set(range(len(case.relevant_documents)))
            ranked = sorted(
                range(len(documents)),
                key=lambda index: (
                    -cosine(
                        query,
                        provider.embed_text(
                            documents[index],
                            purpose="evaluation_document",
                        ).as_array()[0],
                    ),
                    index,
                ),
            )
            first_relevant_rank = min(ranked.index(index) + 1 for index in relevant)
            reciprocal_ranks.append(1.0 / first_relevant_rank)
            recall_at_1 += bool(relevant & set(ranked[:1]))
            recall_at_3 += bool(relevant & set(ranked[:3]))
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6),
            "recall_at_1": round(recall_at_1 / len(cases), 6),
            "recall_at_3": round(recall_at_3 / len(cases), 6),
            "latency_ms": round(elapsed, 3),
        }

    @staticmethod
    def _challenger(
        key: str,
        *,
        modality: Literal["text", "image_text"],
    ) -> NeuralChallenger:
        challenger = next((row for row in CHALLENGERS if row.key == key), None)
        if challenger is None:
            raise LookupError(f"unknown neural challenger {key!r}")
        if challenger.modality != modality or not challenger.adapter_available:
            raise OptionalProviderError(
                f"challenger {key!r} has no available {modality} evaluation adapter"
            )
        return challenger

    def _hardware(self, model_path: Path) -> dict[str, object]:
        size_bytes = sum(path.stat().st_size for path in model_path.rglob("*") if path.is_file())
        return {
            "device": self.settings.embedding_device,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "model_directory_bytes": size_bytes,
            "batch_size": self.settings.embedding_batch_size,
        }
