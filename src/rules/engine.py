"""
Rule Engine - ports the memecoin-trading-robot-rules skill into Python.

Every rule returns a Verdict: passed (bool), tier (A/B/C), and reason list.
This is the SINGLE source of truth for what the agent considers a buy signal.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Tier(str, Enum):
    """Alert priority tier."""
    A = "A"  # Rule pass + tracked wallet buy
    B = "B"  # Rule pass only
    C = "C"  # Watch / soft signal


@dataclass
class Verdict:
    """Result of evaluating a token against the rules."""
    token_address: str
    symbol: str
    passed: bool
    tier: Tier
    score: float  # 0-100, higher = stronger signal
    reasons: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    strategy: Optional[str] = None  # which strategy triggered
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_alert(self) -> str:
        """Format verdict as a Telegram-ready alert."""
        emoji = {"A": "🟢", "B": "🟡", "C": "🔴"}.get(self.tier.value, "⚪")
        lines = [
            f"{emoji} TIER {self.tier.value} | Score {self.score:.0f}/100",
            f"Token: ${self.symbol} ({self.token_address[:8]}...)",
            f"Strategy: {self.strategy or 'N/A'}",
        ]
        if self.data.get("mcap_usd"):
            lines.append(f"MCAP: ${self.data['mcap_usd']:,.0f}")
        if self.data.get("volume_5m_usd"):
            lines.append(f"5m vol: ${self.data['volume_5m_usd']:,.0f}")
        if self.data.get("liquidity_usd"):
            lines.append(f"Liquidity: ${self.data['liquidity_usd']:,.0f}")
        if self.data.get("top10_pct") is not None:
            lines.append(f"Top-10 holders: {self.data['top10_pct']:.1f}%")
        if self.data.get("insider_pct") is not None:
            lines.append(f"Insiders: {self.data['insider_pct']:.1f}%")
        if self.data.get("age_minutes") is not None:
            lines.append(f"Age: {self.data['age_minutes']:.0f} min")
        if self.data.get("volume_spike_ratio") is not None:
            lines.append(f"Vol spike: {self.data['volume_spike_ratio']:.1f}x")
        if self.data.get("tracked_wallet_buy"):
            lines.append(f"🐋 Tracked wallet: {self.data['tracked_wallet_buy'][:8]}...")
        if self.reasons:
            lines.append(f"✅ {', '.join(self.reasons[:3])}")
        if self.failures:
            lines.append(f"⚠️  {', '.join(self.failures[:3])}")
        return "\n".join(lines)


class RuleEngine:
    """Applies the rulebook to incoming token data."""

    def __init__(self, rules_cfg: Dict[str, Any]):
        self.cfg = rules_cfg

    # ------------------------------------------------------------------
    # STRATEGY 2.1: Pre-migration sniping (pump.fun)
    # ------------------------------------------------------------------
    def evaluate_pre_migration(self, token: Dict[str, Any]) -> Verdict:
        cfg = self.cfg.get("pre_migration_sniping", {})
        if not cfg.get("enabled", True):
            return self._skip("pre-migration disabled")

        mcap = token.get("mcap_usd", 0)
        if not (cfg["min_mcap_usd"] <= mcap <= cfg["max_mcap_usd"]):
            return Verdict(
                token_address=token["address"], symbol=token.get("symbol", "?"),
                passed=False, tier=Tier.C, score=0,
                failures=[f"MCAP ${mcap:,.0f} outside {cfg['min_mcap_usd']}-{cfg['max_mcap_usd']}"]
            )

        reasons, failures, score = [], [], 50

        # Age requirement
        age_min = token.get("age_minutes", 0)
        if age_min < cfg["min_coin_age_minutes"]:
            failures.append(f"too young ({age_min:.0f}min < {cfg['min_coin_age_minutes']})")
            score -= 20
        else:
            reasons.append(f"age {age_min:.0f}min ≥ {cfg['min_coin_age_minutes']}")
            score += 5

        # Holder concentration
        top10 = token.get("top10_pct", 100)
        if top10 > cfg["max_top10_holder_pct"]:
            failures.append(f"top-10 holders {top10:.1f}% > {cfg['max_top10_holder_pct']}%")
            score -= 25
        else:
            reasons.append(f"top-10 {top10:.1f}% ≤ {cfg['max_top10_holder_pct']}%")
            score += 10

        # Insider %
        insider = token.get("insider_pct", 0)
        if insider > cfg["max_insider_pct"]:
            failures.append(f"insiders {insider:.1f}% > {cfg['max_insider_pct']}%")
            score -= 20
        else:
            reasons.append(f"insiders {insider:.1f}% ≤ {cfg['max_insider_pct']}%")
            score += 5

        # Bundle %
        bundle = token.get("bundle_pct", 0)
        if bundle > cfg["max_bundle_pct"]:
            failures.append(f"bundling {bundle:.1f}% > {cfg['max_bundle_pct']}% (rug risk)")
            score -= 30
        else:
            reasons.append(f"bundling {bundle:.1f}% ≤ {cfg['max_bundle_pct']}%")
            score += 10

        # Pro-trader count
        pro_traders = token.get("pro_traders", 0)
        if pro_traders < cfg["min_pro_traders"]:
            failures.append(f"pro-traders {pro_traders} < {cfg['min_pro_traders']}")
            score -= 15
        else:
            reasons.append(f"pro-traders {pro_traders} ≥ {cfg['min_pro_traders']}")
            score += 10

        passed = len(failures) <= 1 and score >= 40
        tier = Tier.B if passed else Tier.C

        return Verdict(
            token_address=token["address"], symbol=token.get("symbol", "?"),
            passed=passed, tier=tier, score=max(0, min(100, score)),
            reasons=reasons, failures=failures, strategy="pre_migration",
            data=token
        )

    # ------------------------------------------------------------------
    # STRATEGY 2.2: Pullback trading (established coins)
    # Updated per video 2.5: skip MCAP<$100k, target 2x in 4-6h
    # ------------------------------------------------------------------
    def evaluate_pullback(self, token: Dict[str, Any]) -> Verdict:
        cfg = self.cfg.get("pullback_trading", {})
        if not cfg.get("enabled", True):
            return self._skip("pullback disabled")

        mcap = token.get("mcap_usd", 0)
        if mcap < cfg["min_mcap_usd"]:
            return Verdict(
                token_address=token["address"], symbol=token.get("symbol", "?"),
                passed=False, tier=Tier.C, score=0,
                failures=[f"MCAP ${mcap:,.0f} < ${cfg['min_mcap_usd']:,.0f} (dead per video 2.5)"]
            )

        reasons, failures, score = [], [], 50

        # 5m volume floor
        vol5m = token.get("volume_5m_usd", 0)
        if vol5m < cfg["min_5m_volume_usd"]:
            failures.append(f"5m vol ${vol5m:,.0f} < ${cfg['min_5m_volume_usd']:,.0f}")
            score -= 20
        else:
            reasons.append(f"5m vol ${vol5m:,.0f}")
            score += 10

        # Liquidity floor
        liq = token.get("liquidity_usd", 0)
        if liq < cfg.get("liquidity_floor_usd", 50000):
            failures.append(f"liquidity ${liq:,.0f} < ${cfg.get('liquidity_floor_usd', 50000):,.0f}")
            score -= 15
        else:
            reasons.append(f"liquidity ${liq:,.0f}")
            score += 5

        # Buy/sell ratio (momentum)
        bs_ratio = token.get("buy_sell_ratio_m5", 0)
        if bs_ratio > 0 and bs_ratio < cfg.get("buy_sell_ratio_min", 1.5):
            failures.append(f"buy/sell {bs_ratio:.2f} < {cfg.get('buy_sell_ratio_min', 1.5)} (selling pressure)")
            score -= 20
        elif bs_ratio >= cfg.get("buy_sell_ratio_min", 1.5):
            reasons.append(f"buy/sell {bs_ratio:.2f}x (bullish)")
            score += 10

        # Price change range (avoid late entries & dying coins)
        pc_m5 = token.get("price_change_m5_pct", 0)
        if pc_m5 > cfg.get("price_change_5m_max_pct", 50):
            failures.append(f"m5 pump {pc_m5:+.1f}% (already late)")
            score -= 15
        elif pc_m5 < cfg.get("price_change_5m_min_pct", -30):
            failures.append(f"m5 dump {pc_m5:+.1f}% (dying)")
            score -= 15
        elif pc_m5 < 0:
            reasons.append(f"pullback {pc_m5:+.1f}% (potential dip-buy)")
            score += 15

        # Fib retracement entry
        fib_level = token.get("fib_retracement", 0)
        if fib_level in cfg["fib_entry_levels"]:
            reasons.append(f"Fib bounce at {fib_level:.3f}")
            score += 15
        elif fib_level > 0:
            failures.append(f"not at Fib level ({fib_level:.2f})")
            score -= 5

        # Pro-trader floor
        pro_traders = token.get("pro_traders", 0)
        if pro_traders < cfg["min_pro_traders"]:
            failures.append(f"pro-traders {pro_traders} < {cfg['min_pro_traders']}")
            score -= 15
        else:
            score += 5

        passed = len(failures) == 0 and score >= 55
        tier = Tier.B if passed else Tier.C

        return Verdict(
            token_address=token["address"], symbol=token.get("symbol", "?"),
            passed=passed, tier=tier, score=max(0, min(100, score)),
            reasons=reasons, failures=failures, strategy="pullback",
            data=token
        )

    # ------------------------------------------------------------------
    # MOMENTUM FILTER (video 2.5: only "active" coins)
    # ------------------------------------------------------------------
    def passes_momentum_filter(self, token: Dict[str, Any]) -> tuple[bool, List[str]]:
        """Apply the momentum filter — is this coin actually moving?"""
        cfg = self.cfg.get("momentum_filter", {})
        if not cfg.get("enabled", True):
            return True, []

        failures = []

        # Buy/sell ratio
        bs = token.get("buy_sell_ratio_m5", 0)
        if bs > 0 and bs < cfg.get("min_buy_sell_ratio_m5", 1.5):
            failures.append(f"more sells than buys ({bs:.2f}x)")

        # 1h volume floor
        h1 = token.get("volume_1h_usd", 0)
        if h1 < cfg.get("min_h1_volume_usd", 50000):
            failures.append(f"1h vol ${h1:,.0f} < ${cfg.get('min_h1_volume_usd', 50000):,.0f} (not pumping)")

        # 1h transaction count
        txns_h1 = token.get("txns_1h_buys", 0) + token.get("txns_1h_sells", 0)
        if txns_h1 < cfg.get("min_txns_h1", 20):
            failures.append(f"only {txns_h1} txns/h (dead)")

        # FDV/MCAP ratio
        fmr = token.get("fdv_mcap_ratio", 0)
        if fmr > cfg.get("max_fdv_mcap_ratio", 10.0):
            failures.append(f"FDV/MCAP {fmr:.1f}x (heavy unlocks)")

        return (len(failures) == 0, failures)

    # ------------------------------------------------------------------
    # STRATEGY 2.4: Volume spike detection
    # ------------------------------------------------------------------
    def evaluate_volume_spike(self, token: Dict[str, Any]) -> Verdict:
        cfg = self.cfg.get("volume_spike", {})
        if not cfg.get("enabled", True):
            return self._skip("volume spike disabled")

        reasons, failures, score = [], [], 60

        # Spike ratio (5m vol / baseline)
        spike = token.get("volume_spike_ratio", 0)
        if spike < cfg["min_volume_spike_ratio"]:
            failures.append(f"vol spike {spike:.1f}x < {cfg['min_volume_spike_ratio']}x")
            score -= 30
        else:
            reasons.append(f"vol spike {spike:.1f}x (potential {cfg['potential_multiple']})")
            score += 20

        # Liquidity cap (small caps = big upside)
        liq = token.get("liquidity_usd", float("inf"))
        if liq > cfg["max_liquidity_sol"]:
            failures.append(f"liquidity ${liq:,.0f} > ${cfg['max_liquidity_sol']:,.0f} (too deep)")
            score -= 10
        else:
            reasons.append(f"liquidity ${liq:,.0f} (runner-sized)")
            score += 10

        # Volume decay check (rule 2.4 exit logic - applies to entry filter)
        decay = self.check_volume_decay(token)
        if decay["decaying"]:
            failures.append(f"volume decaying: {decay['reason']}")
            score -= 20

        passed = len(failures) <= 1 and score >= 50
        tier = Tier.B if passed else Tier.C

        return Verdict(
            token_address=token["address"], symbol=token.get("symbol", "?"),
            passed=passed, tier=tier, score=max(0, min(100, score)),
            reasons=reasons, failures=failures, strategy="volume_spike",
            data={**token, "volume_spike_ratio": spike}
        )

    # ------------------------------------------------------------------
    # STRATEGY 2.4: Volume decay exit signal
    # ------------------------------------------------------------------
    def check_volume_decay(self, token: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.cfg.get("volume_decay_exit", {})
        if not cfg.get("enabled", True):
            return {"decaying": False, "reason": "disabled"}

        vol1m = token.get("volume_1m_usd", 0)
        vol5m = token.get("volume_5m_usd", 0)
        vol15m = token.get("volume_15m_usd", 0)

        if vol1m > 0:
            ratio_5m_to_1m = vol5m / (vol1m * 5)  # normalize
            if ratio_5m_to_1m < cfg["min_5m_to_1m_ratio"]:
                return {"decaying": True, "reason": f"5m/1m = {ratio_5m_to_1m:.2f} < {cfg['min_5m_to_1m_ratio']}"}

        if vol15m > 0:
            ratio_5m_to_15m = vol5m / (vol15m / 3)  # normalize 15m -> 5m baseline
            if ratio_5m_to_15m < cfg["min_5m_to_15m_ratio"]:
                return {"decaying": True, "reason": f"5m/15m = {ratio_5m_to_15m:.2f} < {cfg['min_5m_to_15m_ratio']}"}

        return {"decaying": False, "reason": "volume healthy"}

    # ------------------------------------------------------------------
    # Wallet signal: tracked wallet just bought
    # ------------------------------------------------------------------
    def apply_wallet_signal(self, verdict: Verdict, wallet: str, tier: int = 3) -> Verdict:
        """Upgrade verdict based on tracked wallet activity.

        Tier 1 (top 10% wallets): strong boost → +35 score, force Tier A
        Tier 2 (next 20%):       medium boost → +20 score, force Tier A
        Tier 3 (rest):            light boost → +10 score, only if passed

        If verdict didn't pass the rules, even Tier 1 won't force Tier A
        (we don't want to alert on tokens that failed every other filter).
        """
        if tier == 1:
            score_boost = 35
            force_tier_a = True
        elif tier == 2:
            score_boost = 20
            force_tier_a = True
        else:
            score_boost = 10
            force_tier_a = False

        if not verdict.passed:
            # Track the wallet signal but don't promote
            verdict.reasons.append(f"tracked wallet {wallet[:8]}... (T{tier}) buying but rules failed")
            verdict.data["tracked_wallet_buy"] = wallet
            verdict.data["wallet_tier"] = tier
            return verdict

        if force_tier_a:
            verdict.tier = Tier.A

        verdict.score = min(100, verdict.score + score_boost)
        tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(tier, "📌")
        verdict.reasons.insert(0, f"{tier_emoji} T{tier} wallet {wallet[:8]}... buying")
        verdict.data["tracked_wallet_buy"] = wallet
        verdict.data["wallet_tier"] = tier
        return verdict

    # ------------------------------------------------------------------
    # Master entry point
    # ------------------------------------------------------------------
    def evaluate(self, token: Dict[str, Any]) -> Verdict:
        """Run all enabled strategies, return the best verdict."""
        candidates = [
            self.evaluate_pre_migration(token),
            self.evaluate_pullback(token),
            self.evaluate_volume_spike(token),
        ]
        # Filter to those that passed
        passed = [v for v in candidates if v.passed]
        if not passed:
            # Return the highest-scoring failure for transparency
            return max(candidates, key=lambda v: v.score)
        # Return the highest-scoring pass
        return max(passed, key=lambda v: v.score)

    def _skip(self, reason: str) -> Verdict:
        return Verdict(
            token_address="", symbol="", passed=False, tier=Tier.C, score=0,
            failures=[reason]
        )
