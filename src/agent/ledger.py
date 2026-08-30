"""
Paper trading ledger - tracks hypothetical entries/exits for tuning.

Persists to data/paper_ledger.json. No real money involved.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PROJECT_ROOT / "data" / "paper_ledger.json"


@dataclass
class PaperPosition:
    symbol: str
    address: str
    strategy: str
    entry_mcap_usd: float
    entry_price_usd: float
    entry_time: float
    tier: str
    score: float
    size_usd: float
    take_profit_1_pct: float = 100.0
    stop_loss_pct: float = -20.0
    exit_mcap_usd: Optional[float] = None
    exit_price_usd: Optional[float] = None
    exit_time: Optional[float] = None
    pnl_pct: Optional[float] = None
    status: str = "open"  # open, closed_tp, closed_sl, closed_manual

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PaperLedger:
    def __init__(self, starting_capital: float = 10_000.0, position_pct: float = 5.0):
        self.starting_capital = starting_capital
        self.position_pct = position_pct / 100.0
        self.positions: List[PaperPosition] = []
        self._load()

    def _load(self):
        if LEDGER_PATH.exists():
            try:
                data = json.loads(LEDGER_PATH.read_text())
                self.positions = [PaperPosition(**p) for p in data.get("positions", [])]
            except Exception:
                self.positions = []

    def _save(self):
        LEDGER_PATH.parent.mkdir(exist_ok=True)
        LEDGER_PATH.write_text(json.dumps({
            "starting_capital": self.starting_capital,
            "position_pct": self.position_pct,
            "positions": [p.to_dict() for p in self.positions],
        }, indent=2))

    def open_position(self, symbol: str, address: str, strategy: str,
                      mcap_usd: float, price_usd: float, tier: str, score: float,
                      take_profit_pct: float = 100.0, stop_loss_pct: float = -20.0) -> PaperPosition:
        size = self.starting_capital * self.position_pct
        pos = PaperPosition(
            symbol=symbol, address=address, strategy=strategy,
            entry_mcap_usd=mcap_usd, entry_price_usd=price_usd,
            entry_time=time.time(), tier=tier, score=score, size_usd=size,
            take_profit_1_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
        )
        self.positions.append(pos)
        self._save()
        return pos

    def close_position(self, address: str, exit_price_usd: float, reason: str = "manual") -> Optional[PaperPosition]:
        for p in self.positions:
            if p.address == address and p.status == "open":
                p.exit_price_usd = exit_price_usd
                p.exit_time = time.time()
                p.pnl_pct = ((exit_price_usd - p.entry_price_usd) / p.entry_price_usd) * 100
                p.status = f"closed_{reason}"
                self._save()
                return p
        return None

    def update_open_positions(self, current_mcap_by_address: Dict[str, float]):
        """Mark TP/SL based on current prices."""
        changed = False
        for p in self.positions:
            if p.status != "open":
                continue
            cur = current_mcap_by_address.get(p.address)
            if cur is None:
                continue
            # Use mcap as proxy for price
            pnl_pct = ((cur - p.entry_mcap_usd) / p.entry_mcap_usd) * 100
            if pnl_pct >= p.take_profit_1_pct:
                p.exit_mcap_usd = cur
                p.exit_time = time.time()
                p.pnl_pct = pnl_pct
                p.status = "closed_tp"
                changed = True
            elif pnl_pct <= p.stop_loss_pct:
                p.exit_mcap_usd = cur
                p.exit_time = time.time()
                p.pnl_pct = pnl_pct
                p.status = "closed_sl"
                changed = True
        if changed:
            self._save()

    def stats(self) -> Dict[str, Any]:
        closed = [p for p in self.positions if p.status.startswith("closed_")]
        wins = [p for p in closed if (p.pnl_pct or 0) > 0]
        losses = [p for p in closed if (p.pnl_pct or 0) <= 0]
        total_pnl = sum((p.pnl_pct or 0) * p.size_usd / 100 for p in closed)
        return {
            "open": len([p for p in self.positions if p.status == "open"]),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed) * 100) if closed else 0,
            "total_pnl_usd": total_pnl,
            "avg_win_pct": sum(p.pnl_pct for p in wins) / len(wins) if wins else 0,
            "avg_loss_pct": sum(p.pnl_pct for p in losses) / len(losses) if losses else 0,
        }
