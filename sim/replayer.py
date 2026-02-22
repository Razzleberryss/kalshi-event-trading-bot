"""sim/replayer.py - Historical backtesting replayer.

Loads historical market snapshots from CSV files and replays them
through a PredictionModel + decision logic to compute P&L metrics.

CSV schema expected:
    timestamp, ticker, yes_bid, yes_ask, last_price, volume, open_interest
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config
from model.interface import PredictionModel

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    timestamp: datetime
    ticker: str
    side: str
    action: str
    price_cents: int
    model_prob: float
    implied_prob: float
    edge: float
    resolved_yes: Optional[bool] = None  # filled in during settlement
    pnl_cents: int = 0


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_cents: int = 0
    max_drawdown_cents: int = 0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)

    def summarise(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl_dollars": round(self.total_pnl_cents / 100, 2),
            "max_drawdown_dollars": round(self.max_drawdown_cents / 100, 2),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
        }


class HistoricalReplayer:
    """Replays historical market data through a prediction model.

    Usage:
        replayer = HistoricalReplayer(model=ExampleHeuristicModel())
        result = replayer.run(csv_path="data/snapshots.csv")
        print(result.summarise())
    """

    def __init__(
        self,
        model: PredictionModel,
        edge_threshold: Optional[float] = None,
        min_confidence: Optional[float] = None,
    ) -> None:
        self._model = model
        self._edge_threshold = edge_threshold or config.min_edge_threshold
        self._min_confidence = min_confidence or config.min_confidence

    def run(self, csv_path: str | Path) -> BacktestResult:
        """Run backtest on historical CSV snapshot file."""
        rows = self._load_csv(csv_path)
        if not rows:
            logger.warning("No rows loaded from %s", csv_path)
            return BacktestResult()

        logger.info(
            "Starting backtest: %d rows from %s | edge=%.3f conf=%.3f",
            len(rows), csv_path, self._edge_threshold, self._min_confidence,
        )

        result = BacktestResult()
        cumulative_pnl = 0
        peak_pnl = 0
        pnl_series: List[int] = []

        for row in rows:
            ticker = row.get("ticker", "")
            if not ticker:
                continue

            snapshot = {
                "yes_bid": int(row.get("yes_bid", 0)),
                "yes_ask": int(row.get("yes_ask", 0)),
                "last_price": int(row.get("last_price", 0)),
                "volume": int(row.get("volume", 0)),
                "open_interest": int(row.get("open_interest", 0)),
            }
            event = {"id": ticker, "ticker": ticker}

            try:
                prediction = self._model.predict(event, snapshot)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Model failed on row %s: %s", ticker, exc)
                continue

            probability = prediction.get("probability", 0.5)
            confidence = prediction.get("confidence", 0.0)

            yes_bid = snapshot["yes_bid"] / 100.0
            yes_ask = snapshot["yes_ask"] / 100.0
            implied_prob = (yes_bid + yes_ask) / 2.0 if yes_ask > 0 else 0.5
            edge = probability - implied_prob

            if edge <= self._edge_threshold or confidence < self._min_confidence:
                continue

            # Simulate buying YES at the ask
            buy_price = snapshot["yes_ask"]
            ts_str = row.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                ts = datetime.utcnow()

            trade = BacktestTrade(
                timestamp=ts,
                ticker=ticker,
                side="yes",
                action="buy",
                price_cents=buy_price,
                model_prob=probability,
                implied_prob=implied_prob,
                edge=edge,
            )

            # Settle: use last_price as proxy for resolution
            last_price = snapshot["last_price"]
            if last_price >= 99:  # resolved YES
                trade.resolved_yes = True
                trade.pnl_cents = 100 - buy_price  # won
                result.winning_trades += 1
            elif last_price <= 1:  # resolved NO
                trade.resolved_yes = False
                trade.pnl_cents = -buy_price  # lost
                result.losing_trades += 1
            else:
                # Mark-to-market: not settled yet
                trade.pnl_cents = last_price - buy_price

            result.total_trades += 1
            result.total_pnl_cents += trade.pnl_cents
            result.trades.append(trade)

            cumulative_pnl += trade.pnl_cents
            peak_pnl = max(peak_pnl, cumulative_pnl)
            pnl_series.append(cumulative_pnl)

        # Max drawdown
        if pnl_series:
            peak = 0
            max_dd = 0
            for val in pnl_series:
                peak = max(peak, val)
                dd = peak - val
                max_dd = max(max_dd, dd)
            result.max_drawdown_cents = max_dd

        # Win rate
        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades

        # Simple Sharpe approximation (mean/std of per-trade P&L)
        if len(result.trades) > 1:
            pnls = [t.pnl_cents for t in result.trades]
            mean = sum(pnls) / len(pnls)
            variance = sum((x - mean) ** 2 for x in pnls) / len(pnls)
            std = variance ** 0.5
            result.sharpe_ratio = (mean / std) if std > 0 else 0.0

        logger.info("Backtest complete: %s", result.summarise())
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_csv(path: str | Path) -> List[Dict[str, str]]:
        """Load CSV file rows into a list of dicts."""
        p = Path(path)
        if not p.exists():
            logger.error("CSV file not found: %s", p)
            return []
        rows = []
        with p.open(newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(dict(row))
        logger.debug("Loaded %d rows from %s", len(rows), p)
        return rows
