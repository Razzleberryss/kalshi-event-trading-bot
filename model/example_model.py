"""model/example_model.py - Baseline heuristic prediction model.

This model makes simple rule-based predictions to get you started.
Replace or extend with your real alpha model (ML, NLP sentiment, etc.).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from model.interface import PredictionModel

logger = logging.getLogger(__name__)


class ExampleHeuristicModel(PredictionModel):
    """A dead-simple baseline model.

    Strategy:
        - If market implied probability is extreme (< 5% or > 95%), fade it slightly.
        - For liquid markets (volume > threshold), trust the market more.
        - Confidence scales with volume and open interest.

    This is intentionally simple. Swap in your own ML/NLP model here.
    """

    def __init__(
        self,
        fade_extreme_threshold: float = 0.05,
        fade_amount: float = 0.02,
        volume_confidence_scale: int = 500,
    ) -> None:
        self.fade_extreme_threshold = fade_extreme_threshold
        self.fade_amount = fade_amount
        self.volume_confidence_scale = volume_confidence_scale
        logger.info(
            "ExampleHeuristicModel initialized (fade_threshold=%.2f, fade=%.2f)",
            fade_extreme_threshold,
            fade_amount,
        )

    def predict(
        self,
        event: Dict[str, Any],
        market_snapshot: Dict[str, Any],
    ) -> Dict[str, float]:
        """Produce a heuristic probability and confidence estimate."""
        yes_bid: float = float(market_snapshot.get("yes_bid", 50)) / 100.0
        yes_ask: float = float(market_snapshot.get("yes_ask", 50)) / 100.0
        volume: int = int(market_snapshot.get("volume", 0))
        open_interest: int = int(market_snapshot.get("open_interest", 0))

        # Mid-market implied probability
        implied_prob = (yes_bid + yes_ask) / 2.0
        implied_prob = max(0.01, min(0.99, implied_prob))

        # --- Heuristic 1: Fade extremes ---
        # If market thinks something is near-certain, nudge toward uncertainty.
        if implied_prob < self.fade_extreme_threshold:
            model_prob = implied_prob + self.fade_amount
        elif implied_prob > (1.0 - self.fade_extreme_threshold):
            model_prob = implied_prob - self.fade_amount
        else:
            # No strong view; mirror the market
            model_prob = implied_prob

        model_prob = max(0.01, min(0.99, model_prob))

        # --- Confidence: scales with liquidity ---
        # Thin markets -> low confidence; liquid markets -> higher confidence
        raw_confidence = min(
            1.0, (volume + open_interest) / self.volume_confidence_scale
        )
        # Clamp to a reasonable range so we don't over-trade thin books
        confidence = 0.30 + 0.50 * raw_confidence  # range: [0.30, 0.80]

        logger.debug(
            "[%s] event=%s implied=%.3f model_prob=%.3f confidence=%.3f vol=%d oi=%d",
            self.name,
            event.get("id", "unknown"),
            implied_prob,
            model_prob,
            confidence,
            volume,
            open_interest,
        )

        return {
            "probability": round(model_prob, 4),
            "confidence": round(confidence, 4),
        }

    def warm_up(self) -> None:
        logger.info("%s warm-up complete (no external data needed).", self.name)
