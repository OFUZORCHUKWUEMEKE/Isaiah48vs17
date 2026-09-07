"""
Decision journal - stage 1 of docs/intelligence-plan.md.

Records the full feature snapshot + verdict for every token the bot
evaluates, pass or fail, so future stages can measure (rather than
guess) whether a rule or threshold is actually any good. Writes nothing
that changes runtime behavior; a journaling failure must never break a
scan cycle.

Appends one JSON object per line to data/decisions.jsonl (JSON Lines -
cheap to append, cheap to stream-read later without loading the whole
file, unlike PaperLedger's load-whole-file-then-rewrite approach, which
is fine for a few hundred positions but wrong for what will become a
high-volume, mostly-write-only log).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

log = get_logger("journal")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PATH = PROJECT_ROOT / "data" / "decisions.jsonl"

# The exact set of fields RuleEngine reads off a token dict (see
# src/rules/engine.py's evaluate_pre_migration/evaluate_pullback/
# evaluate_volume_spike/passes_momentum_filter) - kept as an explicit
# allowlist so the journal has a stable schema instead of dumping every
# ad-hoc key a data source happens to attach to a token dict (dex,
# chain, gmgn_source, etc. - noise for this purpose).
FEATURE_KEYS = [
    "age_minutes",
    "bundle_pct",
    "buy_sell_ratio_m5",
    "fdv_mcap_ratio",
    "fib_retracement",
    "insider_pct",
    "liquidity_usd",
    "mcap_usd",
    "price_change_m5_pct",
    "pro_traders",
    "top10_pct",
    "txns_1h_buys",
    "txns_1h_sells",
    "volume_15m_usd",
    "volume_1h_usd",
    "volume_1m_usd",
    "volume_5m_usd",
    "volume_spike_ratio",
]


def _extract_features(token: Dict[str, Any]) -> Dict[str, Any]:
    return {k: token.get(k) for k in FEATURE_KEYS if k in token}


class DecisionJournal:
    def __init__(self, path: Path = JOURNAL_PATH):
        self.path = path
        self._count = 0

    def record_momentum_reject(self, token: Dict[str, Any], failures: List[str]) -> None:
        """Token failed passes_momentum_filter before reaching the rule
        engine. Recorded pre-enrichment-complete, so the feature set here
        may be thinner than an 'evaluated' record's - that's expected,
        not a bug, and stage 3's report should treat these separately.
        """
        self._append({
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "kind": "momentum_reject",
            "address": token.get("address", ""),
            "symbol": token.get("symbol", "?"),
            "passed": False,
            "tier": None,
            "score": None,
            "strategy": None,
            "reasons": [],
            "failures": list(failures),
            "features": _extract_features(token),
        })

    def record_verdict(self, token: Dict[str, Any], verdict: Any) -> None:
        """Token reached RuleEngine.evaluate() and produced a real Verdict
        (pass or fail - evaluate() always returns the highest-scoring
        candidate across strategies, never None).
        """
        self._append({
            "id": str(uuid.uuid4()),
            "ts": verdict.timestamp,
            "kind": "evaluated",
            "address": verdict.token_address or token.get("address", ""),
            "symbol": verdict.symbol or token.get("symbol", "?"),
            "passed": verdict.passed,
            "tier": verdict.tier.value if verdict.tier else None,
            "score": verdict.score,
            "strategy": verdict.strategy,
            "reasons": list(verdict.reasons),
            "failures": list(verdict.failures),
            "features": _extract_features(token),
        })

    def _append(self, record: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
            self._count += 1
        except Exception as e:
            # Journaling is observability, not the trading path - a
            # write failure here must never take down a scan cycle.
            log.error(f"Failed to write decision journal record: {e}")

    def stats(self) -> Dict[str, Any]:
        return {
            "records_this_run": self._count,
            "path": str(self.path),
            "exists": self.path.exists(),
        }
