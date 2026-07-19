from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field


@dataclass
class EvidenceCandidate:
    entity_type: str
    entity_id: int
    retrieval_channel: str
    raw_score: float
    metadata: dict[str, object] = field(default_factory=dict)
    normalized_score: float = 0.0
    fusion_score: float = 0.0
    recency_score: float = 0.0
    policy_score: float = 0.0
    diversity_penalty: float = 0.0
    duplicate_cluster: str | None = None
    exclusion_reason: str | None = None
    selected: bool = False
    selected_rank: int | None = None
    evidence_role: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return self.entity_type, self.entity_id


def minmax_normalize(rows: Sequence[EvidenceCandidate]) -> None:
    if not rows:
        return
    values = [row.raw_score for row in rows]
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum
    for row in rows:
        row.normalized_score = (
            1.0
            if span == 0 and maximum > 0
            else ((row.raw_score - minimum) / span if span else 0.0)
        )


def reciprocal_rank_fusion(
    pools: Mapping[str, Sequence[EvidenceCandidate]],
    *,
    weights: Mapping[str, float] | None = None,
    rank_constant: float = 60.0,
) -> list[EvidenceCandidate]:
    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    weight_map = dict(weights or {})
    aggregate: dict[tuple[str, int], EvidenceCandidate] = {}
    score_map: defaultdict[tuple[str, int], float] = defaultdict(float)
    channels: defaultdict[tuple[str, int], list[str]] = defaultdict(list)
    for pool_name in sorted(pools):
        rows = list(pools[pool_name])
        minmax_normalize(rows)
        weight = float(weight_map.get(pool_name, 1.0))
        for rank, row in enumerate(
            sorted(rows, key=lambda item: (-item.raw_score, item.entity_id)),
            start=1,
        ):
            aggregate.setdefault(row.key, row)
            score_map[row.key] += weight / (rank_constant + rank)
            channels[row.key].append(pool_name)
    result = []
    for key, row in aggregate.items():
        row.fusion_score = score_map[key]
        row.metadata = {
            **row.metadata,
            "matched_retrieval_channels": sorted(channels[key]),
        }
        result.append(row)
    return sorted(
        result,
        key=lambda item: (
            -item.fusion_score,
            -item.policy_score,
            -item.recency_score,
            item.entity_id,
        ),
    )


def mmr_select(
    rows: Sequence[EvidenceCandidate],
    *,
    limit: int,
    similarity: Callable[[EvidenceCandidate, EvidenceCandidate], float],
    lambda_relevance: float = 0.72,
    role_quotas: Mapping[str, int] | None = None,
) -> list[EvidenceCandidate]:
    if limit < 0:
        raise ValueError("MMR limit cannot be negative")
    if not 0 <= lambda_relevance <= 1:
        raise ValueError("MMR lambda must be between zero and one")
    quotas = dict(role_quotas or {})
    selected: list[EvidenceCandidate] = []
    remaining = [row for row in rows if row.exclusion_reason is None]
    role_counts: defaultdict[str, int] = defaultdict(int)
    while remaining and len(selected) < limit:
        scored: list[tuple[float, float, int, EvidenceCandidate]] = []
        for row in remaining:
            role = row.evidence_role or "unassigned"
            quota = quotas.get(role)
            if quota is not None and role_counts[role] >= quota:
                continue
            redundancy = max((similarity(row, other) for other in selected), default=0.0)
            duplicate_redundancy = (
                1.0
                if row.duplicate_cluster
                and any(other.duplicate_cluster == row.duplicate_cluster for other in selected)
                else 0.0
            )
            redundancy = max(redundancy, duplicate_redundancy)
            row.diversity_penalty = (1.0 - lambda_relevance) * redundancy
            relevance = row.fusion_score + 0.05 * row.policy_score + 0.03 * row.recency_score
            score = lambda_relevance * relevance - row.diversity_penalty
            scored.append((score, relevance, -row.entity_id, row))
        if not scored:
            break
        chosen = max(scored, key=lambda item: item[:3])[3]
        chosen.selected = True
        chosen.selected_rank = len(selected) + 1
        selected.append(chosen)
        role_counts[chosen.evidence_role or "unassigned"] += 1
        remaining.remove(chosen)
    return selected


def role_coverage(rows: Iterable[EvidenceCandidate]) -> dict[str, int]:
    result: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        if row.selected:
            result[row.evidence_role or "unassigned"] += 1
    return dict(sorted(result.items()))
