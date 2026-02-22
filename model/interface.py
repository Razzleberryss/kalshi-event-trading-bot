"""model/interface.py - Abstract base for all prediction models.

Every model the bot uses must implement PredictionModel.
This keeps the execution layer 100% decoupled from alpha logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class PredictionModel(ABC):
    """Interface that every prediction model must satisfy."""

    @abstractmethod
    def predict(
        self,
        event: Dict[str, Any],
        market_snapshot: Dict[str, Any],
    ) -> Dict[str, float]:
        """Return a probability estimate and a confidence score.

        Args:
            event: Dict describing the event (id, title, category, close_time, etc.).
            market_snapshot: Current market state from Kalshi API
                (ticker, yes_bid, yes_ask, last_price, volume, open_interest, etc.).

        Returns:
            A dict with at least:
                {
                    "probability": float,   # 0.0 – 1.0  model's YES probability
                    "confidence": float,    # 0.0 – 1.0  model's confidence in estimate
                }
        """
        ...

    @property
    def name(self) -> str:
        """Human-readable model name for logging."""
        return self.__class__.__name__

    def warm_up(self) -> None:
        """Optional: called once at bot startup to pre-load weights/data."""
        pass

    def tear_down(self) -> None:
        """Optional: called on shutdown to release resources."""
        pass
