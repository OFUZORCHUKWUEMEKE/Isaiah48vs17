"""
Outcome tracker - stage 2 of docs/intelligence-plan.md.

Stage 1 (DecisionJournal) records what the bot decided and why. This
records what actually happened afterward - the piece that turns "we
rejected this" into "we rejected this and it did +340% in an hour", the
only way to ever prove a threshold is wrong instead of guessing.

For every journaled decision (alerted AND rejected), samples price/mcap
at four horizons after the decision and writes the result via
DecisionJournal.record_outcome(). Pure observability, same discipline as
the journal itself: a failure here must never affect the scan loop, and
it never feeds back into a live trading decision on its own (that's
stage 4, and even then only via a proposal a human approves).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

log = get_logger("outcomes")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PENDING_PATH = PROJECT_ROOT / "data" / "pending_outcomes.json"

# Horizons to sample after a decision, in seconds. Kept as a code
# constant rather than a config knob (like gmgn.py's rate-limit
# constants) - these define what "stage 2 data" even means, not a
# per-deployment tuning parameter.
HORIZONS_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
}

# Give up on a horizon after this many failed fetch attempts (e.g. a
# token whose pool was pulled and will never return a pair again) rather
# than retrying it forever and letting the pending queue grow unbounded.
MAX_FETCH_ATTEMPTS = 3


class OutcomeTracker:
    def __init__(self, market_source: Any, journal: Any, path: Path = PENDING_PATH):
        """market_source needs one async method: get_current_market(address)
        -> {"mcap_usd", "price_usd", "liquidity_usd"} | None. Wired to the
        agent's DexScreener client (free, no key, already always-real) so
        outcome sampling works regardless of which GMGN/Birdeye
        credentials happen to be configured.
        """
        self.market_source = market_source
        self.journal = journal
        self.path = path
        self._pending: List[Dict[str, Any]] = self._load()

    def schedule(
        self,
        decision_id: Optional[str],
        address: str,
        symbol: str,
        entry_mcap_usd: Optional[float],
        entry_price_usd: Optional[float],
        decided_at: Optional[float] = None,
    ) -> None:
        """Register the four horizon checkpoints for one journaled
        decision. No-op if decision_id is None (the journal write itself
        failed - nothing valid to attach an outcome to) or address is
        empty (nothing to sample).
        """
        if not decision_id or not address:
            return
        now = decided_at if decided_at is not None else time.time()
        self._pending.append({
            "decision_id": decision_id,
            "address": address,
            "symbol": symbol,
            "decided_at": now,
            "entry_mcap_usd": entry_mcap_usd,
            "entry_price_usd": entry_price_usd,
            "horizons": {
                label: {"due_at": now + seconds, "attempts": 0}
                for label, seconds in HORIZONS_SECONDS.items()
            },
        })
        self._save()

    async def check_due(self) -> int:
        """Sample every horizon that's come due since the last check.
        Returns how many outcome records were written, for logging/tests.
        """
        now = time.time()
        written = 0
        still_pending: List[Dict[str, Any]] = []

        for entry in self._pending:
            due_labels = [
                label for label, h in entry["horizons"].items()
                if h["due_at"] <= now
            ]
            for label in due_labels:
                market = await self._fetch(entry["address"])
                if market is None:
                    entry["horizons"][label]["attempts"] += 1
                    if entry["horizons"][label]["attempts"] < MAX_FETCH_ATTEMPTS:
                        continue  # leave it pending, retry next tick
                    log.debug(
                        f"Giving up on {entry['symbol']} {label} outcome after "
                        f"{MAX_FETCH_ATTEMPTS} failed fetch attempts - recording "
                        "no-pair-found rather than retrying forever"
                    )
                self.journal.record_outcome(
                    entry["decision_id"], label,
                    entry["entry_mcap_usd"], entry["entry_price_usd"],
                    market,
                )
                written += 1
                del entry["horizons"][label]

            if entry["horizons"]:
                still_pending.append(entry)
            # else: every horizon for this decision has been sampled -
            # drop it, nothing left to track.

        self._pending = still_pending
        self._save()
        return written

    async def _fetch(self, address: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.market_source.get_current_market(address)
        except Exception as e:
            log.debug(f"get_current_market({address[:8]}) failed: {e}")
            return None

    async def run_periodic(self, interval_seconds: int = 300) -> None:
        """Background loop, started the same way _daily_summary_loop is -
        see MemecoinAgent.run(). 5-minute default: fine granularity
        against 15m/1h/4h/24h horizons without hammering DexScreener.
        """
        while True:
            try:
                n = await self.check_due()
                if n:
                    log.info(f"Outcome tracker: sampled {n} due horizon(s)")
            except Exception as e:
                log.error(f"Outcome tracker tick failed: {e}")
            await asyncio.sleep(interval_seconds)

    def stats(self) -> Dict[str, Any]:
        return {
            "pending_decisions": len(self._pending),
            "pending_horizon_checks": sum(len(e["horizons"]) for e in self._pending),
            "path": str(self.path),
        }

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except Exception as e:
            log.warning(f"Could not load {self.path}, starting empty: {e}")
            return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._pending, default=str, indent=2))
        except Exception as e:
            log.error(f"Failed to save pending outcomes: {e}")
