"""
Pure K-line math for the GMGN integration (stage 4 of
docs/gmgn-integration-plan.md). No I/O - these take candle lists already
fetched by GMGNClient.get_kline() and derive the values the rule engine
reads: a genuine volume-spike ratio and a genuine Fibonacci retracement,
replacing the constants src/data/dexscreener.py currently fabricates
(volume_spike_ratio always 5.0, volume_15m_usd always 3x the 5m figure).

Candle shape (GMGNClient.get_kline / GMGNClient._mock_kline): a list of
{"time", "open", "close", "high", "low", "volume", "amount"} dicts,
chronologically ordered oldest-first (most recent candle LAST). Per
GMGN's docs, open/close/high/low may arrive as strings; every read here
goes through float() to handle both.
"""
from __future__ import annotations

from typing import Any, Dict, List


def volume_metrics(candles_1m: List[Dict[str, Any]], lookback: int = 60) -> Dict[str, float]:
    """Derive volume figures from 1-minute candles.

    volume_1m_usd: the most recent candle's volume.
    volume_5m_usd / volume_15m_usd: sum of the last 5 / 15 candles (however
      many are actually available, so a short candle list degrades
      gracefully instead of raising).
    volume_spike_ratio: the most recent candle's volume divided by the mean
      of the `lookback` candles BEFORE it (the current candle is excluded
      from its own baseline). This is the actual point of this function:
      dexscreener.py's volume_spike_ratio is algebraically constant at 5.0
      because its "baseline" is derived from the same number it's compared
      against (m5/5, then compared to m5). Here the baseline is a real
      trailing window, so the ratio can vary and the rule engine's
      volume_spike strategy can pass or fail on it for the first time.

    Returns 0.0 for every value when candles_1m is empty, and a
    volume_spike_ratio of 0.0 (never a division by zero) when there is no
    prior-candle baseline to compare against.
    """
    if not candles_1m:
        return {
            "volume_1m_usd": 0.0,
            "volume_5m_usd": 0.0,
            "volume_15m_usd": 0.0,
            "volume_spike_ratio": 0.0,
        }

    vols = [float(c.get("volume", 0) or 0) for c in candles_1m]
    vol_1m = vols[-1]
    vol_5m = sum(vols[-5:])
    vol_15m = sum(vols[-15:])

    baseline_window = vols[:-1][-lookback:] if len(vols) > 1 else []
    baseline_mean = (sum(baseline_window) / len(baseline_window)) if baseline_window else 0.0
    spike_ratio = (vol_1m / baseline_mean) if baseline_mean > 0 else 0.0

    return {
        "volume_1m_usd": vol_1m,
        "volume_5m_usd": vol_5m,
        "volume_15m_usd": vol_15m,
        "volume_spike_ratio": spike_ratio,
    }


def fib_retracement(candles: List[Dict[str, Any]], lookback: int = 288) -> float:
    """Fibonacci retracement of the current price within the recent
    swing high/low, as a 0-1 fraction: 0 = at or above the swing high,
    1 = at or below the swing low, 0.618 = 61.8% of the way back down
    from the high toward the low (a classic "golden ratio" bounce level).

    Uses the high/low of each candle in the lookback window (not just
    closes) so the swing captures intra-candle extremes, and the most
    recent candle's close as "current price". Returns 0.0 when there are
    no candles or the window is degenerate (span <= 0, e.g. a flat price
    or a single candle) - a real bounce needs a real range to bounce
    within, and 0.0 is also what src/agent/loop.py already defaults
    fib_retracement to, so this degrades to today's no-op behavior rather
    than a division error.
    """
    if not candles:
        return 0.0

    window = candles[-lookback:] if lookback else candles
    highs = [float(c.get("high", c.get("close", 0)) or 0) for c in window]
    lows = [float(c.get("low", c.get("close", 0)) or 0) for c in window]
    if not highs or not lows:
        return 0.0

    swing_high = max(highs)
    swing_low = min(lows)
    span = swing_high - swing_low
    if span <= 0:
        return 0.0

    current = float(window[-1].get("close", swing_high) or swing_high)
    retracement = (swing_high - current) / span
    return max(0.0, min(1.0, retracement))
