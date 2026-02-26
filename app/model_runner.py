from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from model.interface import PredictionModel


def implied_yes_probability(snapshot: Dict[str, Any]) -> float:
    yes_bid = float(snapshot.get("yes_bid", 0) or 0) / 100.0
    yes_ask = float(snapshot.get("yes_ask", 0) or 0) / 100.0
    if yes_bid > 0 and yes_ask > 0:
        return max(0.01, min(0.99, (yes_bid + yes_ask) / 2.0))
    if yes_bid > 0:
        return max(0.01, min(0.99, yes_bid))
    if yes_ask > 0:
        return max(0.01, min(0.99, yes_ask))
    return 0.50


def run_model_once(
    model: PredictionModel,
    *,
    event: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], float, float]:
    """Run model prediction and return (prob, conf, implied_prob, latency_s)."""
    implied_prob = implied_yes_probability(snapshot)
    t0 = time.perf_counter()
    pred = model.predict(event, snapshot)
    latency_s = time.perf_counter() - t0
    prob = pred.get("probability")
    conf = pred.get("confidence")
    return (
        float(prob) if prob is not None else None,
        float(conf) if conf is not None else None,
        implied_prob,
        latency_s,
    )
