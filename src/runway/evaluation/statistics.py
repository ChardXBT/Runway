from __future__ import annotations

import math
from collections.abc import Iterable


def wilson_interval(
    successes: int,
    total: int,
    *,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("successes and total must satisfy 0 <= successes <= total")
    if total == 0:
        return (0.0, 1.0)
    proportion = successes / total
    denominator = 1 + z**2 / total
    center = (proportion + z**2 / (2 * total)) / denominator
    margin = (
        z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    )
    return (
        round(max(0.0, center - margin), 6),
        round(min(1.0, center + margin), 6),
    )


def percentile(values: Iterable[float], quantile: float) -> float | None:
    if quantile < 0 or quantile > 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(
        ordered[lower] * (1 - fraction) + ordered[upper] * fraction,
        6,
    )


def preference_summary(results: Iterable[str]) -> dict[str, object]:
    values = list(results)
    wins = values.count("win")
    losses = values.count("loss")
    ties = values.count("tie")
    decisive = wins + losses
    interval = wilson_interval(wins, decisive) if decisive else (0.0, 1.0)
    return {
        "sample_size": len(values),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "decisive_win_rate": round(wins / decisive, 6) if decisive else None,
        "wilson_95": list(interval),
    }
