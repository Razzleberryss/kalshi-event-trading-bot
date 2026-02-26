from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StrategyModes:
    enable_market_making: bool = False
    enable_mispricing: bool = True
    enable_hedging: bool = True


@dataclass(frozen=True)
class ModelGate:
    enabled: bool = False
    min_edge_threshold: float = 0.05
    min_confidence: float = 0.60


@dataclass(frozen=True)
class MarketContext:
    category: str = ""
    existing_position_contracts: int = 0
    market_exposure_cents: int = 0
    category_exposure_cents: int = 0


@dataclass(frozen=True)
class ModelContext:
    probability: Optional[float] = None
    confidence: Optional[float] = None
    implied_probability: Optional[float] = None
