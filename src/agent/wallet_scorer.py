"""
Wallet scoring — computes performance metrics for tracked wallets and
classifies them into tiers (1/2/3) so the agent can weight alerts.

What it does:
  1. Pulls recent txs (default 100) per wallet via Helius
  2. Identifies buy/sell pairs by token mint
  3. Computes win rate, avg ROI, recency, activity
  4. Produces a composite score 0-100
  5. Buckets wallets into Tier 1 (top 10%), Tier 2 (next 20%), Tier 3 (rest)

Caching:
  Scores are written to data/wallet_scores.json and reused across restarts
  until the cache is older than SCORE_TTL (default 24h).

Limitations:
  - Uses only recent txs (100/wallet). Long-term PnL may be off.
  - "Sell" detection is heuristic: looks for tokenTransfers FROM the wallet
    to a non-system address. Sells on Raydium/Orca/Jupiter will surface but
    transfers to other wallets won't be detected as sells.
  - For new wallets (< 10 closed positions), score is reduced to reflect
    low sample size.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utils.logger import get_logger

log = get_logger("wallet_scorer")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCORES_PATH = PROJECT_ROOT / "data" / "wallet_scores.json"

# How long a score is considered valid (24h by default).
SCORE_TTL_SECONDS = 24 * 60 * 60

# Tiers (percentile-based)
TIER1_PCT = 0.10  # top 10%
TIER2_PCT = 0.30  # next 20% (10% + 20% = 30% total)


class WalletScorer:
    """Computes and caches wallet performance scores."""

    def __init__(self, helius_client):
        self.helius = helius_client
        self._scores: Dict[str, Dict[str, Any]] = {}  # wallet -> {score, tier, metrics, ...}
        self._last_full_run: float = 0.0
        self._load_cache()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    def _load_cache(self):
        if SCORES_PATH.exists():
            try:
                self._scores = json.loads(SCORES_PATH.read_text())
                log.info(f"Loaded {len(self._scores)} cached wallet scores")
            except Exception as e:
                log.warning(f"Could not load wallet score cache: {e}")
                self._scores = {}

    def _save_cache(self):
        SCORES_PATH.parent.mkdir(exist_ok=True)
        try:
            SCORES_PATH.write_text(json.dumps(self._scores, indent=2))
        except Exception as e:
            log.error(f"Could not save wallet score cache: {e}")

    def is_cache_fresh(self) -> bool:
        """True if the cache has been fully recomputed within SCORE_TTL_SECONDS."""
        if not self._scores:
            return False
        if self._last_full_run == 0:
            return False
        return (time.time() - self._last_full_run) < SCORE_TTL_SECONDS

    # ------------------------------------------------------------------
    # Single-wallet analysis
    # ------------------------------------------------------------------
    async def score_wallet(self, wallet: str) -> Dict[str, Any]:
        """Compute metrics for one wallet. Returns dict with score, tier, components."""
        if "MOCK" in wallet or "REPLACE" in wallet:
            return self._default_score(wallet, reason="mock_wallet")

        txs = await self.helius.get_wallet_transactions(wallet, limit=100)
        if not txs:
            return self._default_score(wallet, reason="no_transactions")

        metrics = self._compute_metrics(wallet, txs)
        metrics["score"] = self._composite_score(metrics)
        return metrics

    def _compute_metrics(self, wallet: str, txs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Walk txs, pair buys with subsequent sells, derive metrics."""
        # Track open positions: mint -> {buy_ts, buy_amount}
        positions: Dict[str, Dict[str, Any]] = {}
        closed_trades: List[Dict[str, Any]] = []  # {mint, buy_ts, sell_ts, roi_estimate, hold_time_s}

        # Sort ascending by timestamp so we pair oldest buys with newest sells
        txs_sorted = sorted(txs, key=lambda t: t.get("timestamp", 0))

        # First pass: identify buys (tokenTransfers TO this wallet)
        for tx in txs_sorted:
            ts = tx.get("timestamp", 0)
            if not ts:
                continue
            for change in tx.get("tokenTransfers", []) or []:
                mint = change.get("mint")
                if not mint:
                    continue
                # Skip system mints
                if mint in (
                    "So11111111111111111111111111111111111111112",  # SOL
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTD",  # USDC
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
                ):
                    continue
                amount = change.get("tokenAmount", 0) or 0
                if change.get("toUserAccount") == wallet:
                    # BUY: token came in
                    if mint not in positions:
                        positions[mint] = {
                            "buy_ts": ts,
                            "buy_amount": amount,
                            "buy_sig": tx.get("signature"),
                        }
                elif change.get("fromUserAccount") == wallet:
                    # SELL: token went out
                    if mint in positions:
                        pos = positions.pop(mint)
                        # Estimate ROI: we don't have prices, but we can use
                        # amount in vs amount out as a rough proxy.
                        # If sell_amount > buy_amount (same decimals), it
                        # suggests they got more tokens = likely a rug or
                        # reflection. If sell_amount < buy_amount, they sold
                        # a portion. Without prices, this is weak signal.
                        # Better: just track that a sell happened, count it
                        # as "closed" with unknown ROI.
                        closed_trades.append({
                            "mint": mint,
                            "buy_ts": pos["buy_ts"],
                            "sell_ts": ts,
                            "buy_sig": pos.get("buy_sig"),
                            "sell_sig": tx.get("signature"),
                            "buy_amount": pos["buy_amount"],
                            "sell_amount": amount,
                            "hold_time_s": ts - pos["buy_ts"],
                        })

        # Second pass: compute basic trading metrics.
        # We can't reliably compute SOL ROI for Jupiter/Raydium swaps because
        # SOL routes through program-owned intermediary accounts, so the
        # wallet's accountData shows 0 SOL change. Without prices, we can
        # still score on:
        #   - Trade frequency (active vs dormant)
        #   - Hold time (longer holds suggest conviction)
        #   - Sell discipline (do they close positions or let them ride?)
        #   - Token diversity (do they ape 1 coin or spread out?)
        # These are weaker than ROI but still predictive.
        trades_with_hold_time = []
        for trade in closed_trades:
            if trade["hold_time_s"] > 0:
                trades_with_hold_time.append(trade["hold_time_s"])

        # Metrics
        n_closed = len(closed_trades)
        n_open = len(positions)
        wins = 0  # We can't reliably compute wins without prices
        losses = 0
        win_rate = 0.0
        avg_roi = 0.0
        avg_hold_hours = (sum(trades_with_hold_time) / 3600 / len(trades_with_hold_time)) if trades_with_hold_time else 0

        # Token diversity: how many unique mints did they trade?
        unique_mints = set()
        for tx in txs:
            for t in tx.get("tokenTransfers", []):
                m = t.get("mint")
                if m and m not in ("So11111111111111111111111111111111111111112",
                                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTD",
                                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"):
                    unique_mints.add(m)
        token_diversity = len(unique_mints)

        # Recency: when was the last activity?
        if txs:
            last_ts = max(t.get("timestamp", 0) for t in txs)
            days_since_active = (time.time() - last_ts) / 86400 if last_ts else 999
        else:
            days_since_active = 999

        # Activity: tx count in the last 7 days
        week_ago = time.time() - 7 * 86400
        week_txs = sum(1 for t in txs if t.get("timestamp", 0) > week_ago)

        # Specialization: what fraction of recent txs are SWAPs (vs transfers)
        swap_count = sum(1 for t in txs if t.get("type") in ("SWAP", "BUY", "SELL"))
        specialization = (swap_count / len(txs)) if txs else 0

        return {
            "wallet": wallet,
            "tx_count": len(txs),
            "closed_positions": n_closed,
            "open_positions": n_open,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_roi": avg_roi,
            "avg_hold_hours": avg_hold_hours,
            "token_diversity": token_diversity,
            "days_since_active": days_since_active,
            "week_txs": week_txs,
            "specialization": specialization,
            "sample_size": len(closed_trades),  # number of buy→sell pairs we found
            "computed_at": time.time(),
        }

    def _native_sol_change(self, tx: Dict[str, Any], wallet: str) -> Optional[float]:
        """Estimate SOL change for this wallet from a tx. Positive = received SOL, negative = spent SOL.
        Helius exposes this via two fields:
          - accountData[].nativeBalanceChange (lamports, can be + or -)
          - nativeTransfers[] (only contains non-zero SOL moves)
        Returns None if we can't determine.
        """
        # Try accountData first (more reliable, has the wallet's actual change)
        acct = tx.get("accountData") or []
        for entry in acct:
            if entry.get("account") == wallet:
                change = entry.get("nativeBalanceChange")
                if change is not None:
                    return change / 1_000_000_000

        # Fallback: sum up nativeTransfers to/from this wallet
        total = 0
        found = False
        for t in tx.get("nativeTransfers", []) or []:
            amt = t.get("amount", 0) or 0  # lamports
            if t.get("fromUserAccount") == wallet:
                total -= amt
                found = True
            elif t.get("toUserAccount") == wallet:
                total += amt
                found = True
        if found:
            return total / 1_000_000_000
        return None

    def _composite_score(self, m: Dict[str, Any]) -> float:
        """Convert metrics into a 0-100 score.

        We can't compute ROI from on-chain data alone (Jupiter/Raydium route
        SOL through intermediary accounts so user balance change is 0).
        Instead we score on signals that ARE reliable:
          - Activity (25): how many txs/week (more = better signal source)
          - Recency (25): active recently (dormant = no signal)
          - Specialization (20): % of txs that are SWAPs vs transfers
          - Hold discipline (15): how long they hold before selling
          - Token diversity (15): how many different mints they trade
        """
        # Activity (0-25 pts): scale up to 20 txs/week = full marks
        week = m.get("week_txs", 0)
        activity_score = min(25, week * 1.25)  # 20 txs/week = 25 pts

        # Recency (0-25 pts): decay based on days since last activity
        days = m.get("days_since_active", 999)
        if days < 0.5:
            recency = 1.0
        elif days < 1:
            recency = 0.9
        elif days < 3:
            recency = 0.7
        elif days < 7:
            recency = 0.4
        elif days < 14:
            recency = 0.2
        else:
            recency = 0.0
        recency_score = recency * 25

        # Specialization (0-20 pts): high % of SWAPs = focused trader
        spec = m.get("specialization", 0)
        specialization_score = min(20, spec * 22)  # 0.9 spec = 20 pts

        # Hold discipline (0-15 pts): avg hold time in hours
        # Sweet spot is 1-72h (active trading). <1h = noise/scalper, >72h = bag-holder
        hold_h = m.get("avg_hold_hours", 0)
        if hold_h < 0.5:
            hold_score = 3  # too fast, probably noise
        elif hold_h < 1:
            hold_score = 8
        elif hold_h < 6:
            hold_score = 15  # ideal
        elif hold_h < 24:
            hold_score = 12
        elif hold_h < 72:
            hold_score = 8
        elif hold_h < 168:
            hold_score = 5  # bag-holder
        else:
            hold_score = 2

        # Token diversity (0-15 pts): how many unique tokens traded
        # 5-20 is ideal (researcher, not one-coin gambler, not degen)
        div = m.get("token_diversity", 0)
        if div < 3:
            div_score = 3
        elif div < 5:
            div_score = 8
        elif div < 10:
            div_score = 13
        elif div < 20:
            div_score = 15  # ideal
        elif div < 50:
            div_score = 12
        else:
            div_score = 8  # might be a bot, or just very scattered

        # Apply a soft penalty for very low closed-position counts
        # (we want wallets that actually trade, not just hold)
        n_closed = m.get("closed_positions", 0)
        if n_closed < 3:
            sample_penalty = 0.6
        elif n_closed < 10:
            sample_penalty = 0.85
        else:
            sample_penalty = 1.0

        raw = activity_score + recency_score + specialization_score + hold_score + div_score
        return max(0, min(100, raw * sample_penalty))

    def _default_score(self, wallet: str, reason: str) -> Dict[str, Any]:
        """Default score for unscoreable wallets (mock, no data, etc)."""
        return {
            "wallet": wallet,
            "score": 30.0,  # below average, no signal
            "tier": 3,
            "closed_positions": 0,
            "open_positions": 0,
            "win_rate": 0,
            "avg_roi": 0,
            "days_since_active": 999,
            "week_txs": 0,
            "reason": reason,
            "computed_at": time.time(),
        }

    # ------------------------------------------------------------------
    # Bulk scoring
    # ------------------------------------------------------------------
    async def score_all(self, wallets: List[str], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Score all wallets. Uses cache unless force=True.
        Returns {wallet: metrics_dict}.
        """
        now = time.time()
        to_score = []
        if force:
            to_score = list(wallets)
        else:
            for w in wallets:
                cached = self._scores.get(w)
                if not cached:
                    to_score.append(w)
                else:
                    age = now - cached.get("computed_at", 0)
                    if age > SCORE_TTL_SECONDS:
                        to_score.append(w)

        if to_score:
            log.info(f"Scoring {len(to_score)} wallets ({len(wallets) - len(to_score)} cached)...")
            failed = 0
            # Serial scoring. Pacing is enforced inside the Helius client so
            # this stays correct even when another caller is running too.
            for i, w in enumerate(to_score):
                try:
                    metrics = await self.score_wallet(w)
                    self._scores[w] = metrics
                    if (i + 1) % 20 == 0:
                        log.info(f"  scored {i + 1}/{len(to_score)}")
                    await asyncio.sleep(0.15)
                except Exception as e:
                    failed += 1
                    log.error(f"Score failed for {w[:8]}: {e}")
                    # Keep any score we already had. Overwriting it with a
                    # 30/tier-3 placeholder poisons _assign_tiers, which ranks
                    # by percentile - a batch of failures (e.g. a rate-limit
                    # burst) would otherwise silently reshuffle every tier.
                    if w not in self._scores:
                        self._scores[w] = self._default_score(w, reason=f"error: {e}")
            if failed:
                log.warning(
                    f"{failed}/{len(to_score)} wallets failed to score; "
                    "kept previous scores where available"
                )
            self._save_cache()
            self._last_full_run = now

        # Now assign tiers
        self._assign_tiers(wallets)
        return self._scores

    def _assign_tiers(self, wallets: List[str]):
        """Bucket wallets into tiers based on composite score."""
        # Sort by score desc
        valid = [(w, self._scores.get(w, {}).get("score", 0)) for w in wallets]
        valid = [(w, s) for w, s in valid if "MOCK" not in w and "REPLACE" not in w]
        valid.sort(key=lambda x: x[1], reverse=True)

        n = len(valid)
        if n == 0:
            return
        t1_cutoff = max(1, int(n * TIER1_PCT))
        t2_cutoff = max(t1_cutoff + 1, int(n * TIER2_PCT))

        for i, (w, score) in enumerate(valid):
            entry = self._scores.setdefault(w, {})
            if i < t1_cutoff:
                entry["tier"] = 1
            elif i < t2_cutoff:
                entry["tier"] = 2
            else:
                entry["tier"] = 3
            entry["score"] = score
        self._save_cache()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def get_tier(self, wallet: str) -> int:
        """Return tier 1/2/3 for a wallet. Defaults to 3."""
        return self._scores.get(wallet, {}).get("tier", 3)

    def get_score(self, wallet: str) -> float:
        """Return 0-100 score. Defaults to 30."""
        return self._scores.get(wallet, {}).get("score", 30.0)

    def get_tier_breakdown(self) -> Dict[int, int]:
        """Return {tier: count}."""
        out = {1: 0, 2: 0, 3: 0}
        for entry in self._scores.values():
            t = entry.get("tier", 3)
            out[t] = out.get(t, 0) + 1
        return out

    def get_top_wallets(self, limit: int = 20) -> List[Tuple[str, float, int]]:
        """Return top N wallets as [(address, score, tier), ...]."""
        items = [(w, e.get("score", 0), e.get("tier", 3))
                 for w, e in self._scores.items()
                 if "MOCK" not in w and "REPLACE" not in w]
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:limit]
