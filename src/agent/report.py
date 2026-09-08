"""
Calibration report - stage 3 of docs/intelligence-plan.md.

Joins the decision journal (stage 1) with the outcome tracker's samples
(stage 2) and summarizes, per strategy and per horizon, what actually
happened to the tokens the bot alerted on vs. the ones it rejected. This
is read-only analysis over data/decisions.jsonl - it changes no runtime
behavior and proposes nothing; it just answers "is this rule any good"
with real numbers instead of a guess.

A horizon bucket with fewer than MIN_SAMPLES outcomes reports its raw
count only (no averages/win-rate) - a stat from 2 samples is noise, not
a signal, and presenting it as one would recreate the exact
fabricated-confidence problem this whole effort exists to fix.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

log = get_logger("report")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PATH = PROJECT_ROOT / "data" / "decisions.jsonl"

MIN_SAMPLES = 5
HORIZON_ORDER = ["15m", "1h", "4h", "24h"]
# A closed outcome counts as a "win" once mcap is up at least this much
# from entry - arbitrary but explicit, unlike a fabricated number: this
# threshold is a report-display choice, not something fed back into the
# rule engine.
WIN_THRESHOLD_PCT = 20.0


def _read_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.error(f"Failed to read journal for report: {e}")
    return records


def _bucket_key(decision: Dict[str, Any]) -> str:
    """Which group a decision's outcomes should be counted under."""
    if decision["kind"] == "momentum_reject":
        return "rejected: momentum_reject"
    if decision["kind"] == "evaluated":
        if decision.get("passed"):
            strat = decision.get("strategy") or "unknown"
            return f"alerted: {strat}"
        return "rejected: rule_engine"
    return "unknown"


def _summarize(outcomes: List[Dict[str, Any]], min_samples: int) -> Dict[str, Any]:
    changes = [o["pct_change_mcap"] for o in outcomes if o.get("pct_change_mcap") is not None]
    no_pair = sum(1 for o in outcomes if o.get("no_pair_found"))
    n = len(outcomes)
    stat: Dict[str, Any] = {"n": n, "no_pair_found": no_pair}
    if n >= min_samples and changes:
        wins = sum(1 for c in changes if c >= WIN_THRESHOLD_PCT)
        stat["avg_pct_change"] = sum(changes) / len(changes)
        stat["win_rate_pct"] = wins / len(changes) * 100
    return stat


def build_report(path: Path = JOURNAL_PATH, min_samples: int = MIN_SAMPLES) -> Dict[str, Any]:
    """Returns a JSON-serializable dict; commands.py formats it for Telegram."""
    records = _read_records(path)

    decisions_by_id: Dict[str, Dict[str, Any]] = {}
    decision_counts: Dict[str, int] = {}
    outcomes: List[Dict[str, Any]] = []

    for r in records:
        kind = r.get("kind")
        if kind in ("momentum_reject", "evaluated"):
            decisions_by_id[r["id"]] = r
            decision_counts[_bucket_key(r)] = decision_counts.get(_bucket_key(r), 0) + 1
        elif kind == "outcome":
            outcomes.append(r)

    # group[bucket][horizon] -> list of outcome records
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    unmatched = 0
    for o in outcomes:
        decision = decisions_by_id.get(o.get("decision_id"))
        if decision is None:
            unmatched += 1
            continue
        bucket = _bucket_key(decision)
        horizon = o.get("horizon", "?")
        groups.setdefault(bucket, {}).setdefault(horizon, []).append(o)

    by_group = {}
    for bucket, by_horizon in groups.items():
        by_group[bucket] = {
            horizon: _summarize(outs, min_samples) for horizon, outs in by_horizon.items()
        }

    return {
        "decision_counts": decision_counts,
        "total_outcomes": len(outcomes),
        "unmatched_outcomes": unmatched,
        "by_group": by_group,
        "min_samples": min_samples,
        "win_threshold_pct": WIN_THRESHOLD_PCT,
    }


def format_report_text(report: Dict[str, Any]) -> str:
    lines = ["📈 <b>Calibration Report</b>\n"]

    dc = report["decision_counts"]
    if not dc:
        lines.append("<i>No decisions journaled yet.</i>")
        return "\n".join(lines)

    lines.append("<b>Decisions journaled</b>")
    for bucket, n in sorted(dc.items()):
        lines.append(f"  {bucket}: {n}")
    lines.append("")

    by_group = report["by_group"]
    if not by_group:
        lines.append("<i>No outcomes sampled yet - check back once tokens have aged past 15m.</i>")
        return "\n".join(lines)

    min_samples = report["min_samples"]
    win_pct = report["win_threshold_pct"]
    lines.append(f"<b>Outcomes by group</b> (win = mcap +{win_pct:.0f}%, min {min_samples} samples for stats)\n")
    for bucket in sorted(by_group.keys()):
        lines.append(f"<b>{bucket}</b>")
        by_horizon = by_group[bucket]
        for horizon in HORIZON_ORDER:
            if horizon not in by_horizon:
                continue
            stat = by_horizon[horizon]
            n = stat["n"]
            if "avg_pct_change" in stat:
                lines.append(
                    f"  {horizon}: n={n} avg={stat['avg_pct_change']:+.1f}% "
                    f"win_rate={stat['win_rate_pct']:.0f}% "
                    f"no_pair={stat['no_pair_found']}"
                )
            else:
                lines.append(f"  {horizon}: n={n} (need {min_samples}+ for stats)")
        lines.append("")

    if report["unmatched_outcomes"]:
        lines.append(f"<i>{report['unmatched_outcomes']} outcome(s) had no matching decision record.</i>")

    return "\n".join(lines).strip()
