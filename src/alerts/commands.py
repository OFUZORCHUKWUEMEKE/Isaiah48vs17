"""
Telegram bot commands. Registers handlers for /stats, /pause, /resume,
/positions, /help, /wallets, /scan, /alerts, /setcap, /closeall.

Each handler is an async function that takes an args string and returns
the response text. The alerter delivers the response back to the chat.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.alerts.telegram import TelegramAlerter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERTS_LOG = PROJECT_ROOT / "data" / "alerts.log"


def register_commands(alerter: "TelegramAlerter", agent):
    """Wire up all bot commands. `agent` is the MemecoinAgent instance."""

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------
    async def cmd_help(args: str) -> str:
        return (
            "🤖 <b>Memecoin Runner Bot — Commands</b>\n\n"
            "<b>Status &amp; data:</b>\n"
            "/stats — paper P&amp;L and bot status\n"
            "/positions — open paper positions\n"
            "/wallets [top|rescore] — tier breakdown + recent buys\n"
            "/alerts [N] — last N alerts (default 5)\n\n"
            "<b>Actions:</b>\n"
            "/scan — force a scan cycle now\n"
            "/setcap N — set daily alert cap to N\n"
            "/closeall — close all open paper positions\n\n"
            "<b>Control:</b>\n"
            "/pause — stop sending alerts (still tracks)\n"
            "/resume — resume alerts\n"
            "/help — show this message"
        )

    # ------------------------------------------------------------------
    # /stats
    # ------------------------------------------------------------------
    async def cmd_stats(args: str) -> str:
        stats = agent.ledger.stats()
        recent_buys = len(agent._recent_wallet_buys)
        cache_size = len(agent._wallet_cache)
        paused = "⏸️ YES" if alerter.is_paused() else "▶️ NO"
        return (
            f"📊 <b>Bot Status</b>\n\n"
            f"Mode: <code>{agent.cfg['mode']}</code>\n"
            f"Alerts paused: {paused}\n"
            f"Sent today: {alerter._today_count}/{agent.daily_cap}\n\n"
            f"💰 <b>Paper P&amp;L</b>\n"
            f"Open: <b>{stats['open']}</b>\n"
            f"Closed: <b>{stats['closed']}</b>\n"
            f"Wins: {stats['wins']} | Losses: {stats['losses']}\n"
            f"Win rate: <b>{stats['win_rate']:.1f}%</b>\n"
            f"Total P&amp;L: <b>${stats['total_pnl_usd']:+,.2f}</b>\n"
            f"Avg win: {stats['avg_win_pct']:+.1f}% | "
            f"Avg loss: {stats['avg_loss_pct']:+.1f}%\n\n"
            f"🐋 <b>Wallets</b>\n"
            f"Tracked: {len(agent.tracked_wallets)}\n"
            f"Recent buys (10m): <b>{recent_buys}</b>\n"
            f"Cache size: {cache_size}"
        )

    async def cmd_status(args: str) -> str:
        return await cmd_stats(args)

    # ------------------------------------------------------------------
    # /positions
    # ------------------------------------------------------------------
    async def cmd_positions(args: str) -> str:
        positions = [p for p in agent.ledger.positions if p.status == "open"]
        if not positions:
            return "📭 <b>No open positions.</b>"
        lines = [f"📂 <b>Open Positions ({len(positions)})</b>\n"]
        for p in positions[-10:]:  # Last 10
            age_h = (time.time() - p.entry_time) / 3600
            lines.append(
                f"• <b>${p.symbol}</b> "
                f"<code>{p.address[:8]}...</code>\n"
                f"  Strategy: {p.strategy} | Tier {p.tier} | Score {p.score:.0f}\n"
                f"  Entry MCAP: ${p.entry_mcap_usd:,.0f} | "
                f"Size: ${p.size_usd:,.0f}\n"
                f"  Age: {age_h:.1f}h | TP {p.take_profit_1_pct:+.0f}% / "
                f"SL {p.stop_loss_pct:+.0f}%"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # /wallets
    # ------------------------------------------------------------------
    async def cmd_wallets(args: str) -> str:
        # Optional subcommand: /wallets top  → show top 15 by score
        #                       /wallets rescore → force a re-score pass
        sub = args.strip().lower()
        if sub == "rescore":
            result = await agent.score_wallets(force=True)
            bd = result.get("tier_breakdown", {})
            return (
                f"🔄 <b>Wallet re-score complete</b>\n\n"
                f"Scored: {result.get('scored', 0)}\n"
                f"Tiers: 🥇{bd.get(1, 0)} 🥈{bd.get(2, 0)} 🥉{bd.get(3, 0)}"
            )
        if sub == "top":
            top = agent.scorer.get_top_wallets(15)
            if not top:
                return "📊 No wallet scores yet. Run /wallets rescore first."
            lines = ["📊 <b>Top Wallets by Score</b>\n"]
            tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}
            for i, (w, score, tier) in enumerate(top, 1):
                lines.append(
                    f"{i}. {tier_emoji[tier]}T{tier} "
                    f"<code>{w[:8]}...{w[-4:]}</code> "
                    f"Score: <b>{score:.1f}</b>"
                )
            return "\n".join(lines)

        # Default: show tier breakdown + recent buys
        breakdown = agent.scorer.get_tier_breakdown()
        recent = agent._recent_wallet_buys
        cached = len(agent._wallet_cache)
        recent_by_tier = agent._recent_buys_by_tier
        header = (
            f"🐋 <b>Tracked Wallets</b>\n\n"
            f"Total tracked: <b>{len(agent.tracked_wallets)}</b>\n"
            f"<b>Tiers:</b> 🥇{breakdown.get(1, 0)} 🥈{breakdown.get(2, 0)} 🥉{breakdown.get(3, 0)}\n"
            f"Cached tx data: <b>{cached}</b>\n"
            f"Recent buys (10m window): <b>{len(recent)}</b>\n"
            f"  └ with 🥇T1 buys: <b>{sum(1 for ts in recent_by_tier.values() if 'T1' in ts)}</b>\n"
            f"  └ with 🥈T2 buys: <b>{sum(1 for ts in recent_by_tier.values() if 'T2' in ts)}</b>\n"
            f"  └ with 🥉T3 buys: <b>{sum(1 for ts in recent_by_tier.values() if 'T3' in ts)}</b>\n\n"
            f"<i>Try /wallets top or /wallets rescore</i>\n"
        )
        if not recent:
            return header + "<i>No recent buys detected.</i>"
        body_lines = []
        for m, w in list(recent.items())[:10]:
            tier = agent.scorer.get_tier(w)
            tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(tier, "📌")
            body_lines.append(
                f"• {tier_emoji} <code>{w[:8]}...</code> bought "
                f"<code>{m[:8]}...</code>"
            )
        return header + "\n".join(body_lines)

    # ------------------------------------------------------------------
    # /alerts [N] — show last N alerts from the log file
    # ------------------------------------------------------------------
    async def cmd_alerts(args: str) -> str:
        # Parse optional N
        n = 5
        if args.strip().isdigit():
            n = min(int(args.strip()), 20)

        if not ALERTS_LOG.exists():
            return "📜 <b>No alerts logged yet.</b>"

        try:
            content = ALERTS_LOG.read_text()
        except Exception as e:
            return f"⚠️ Could not read alerts log: {e}"

        # Split on the separator we write in _log_to_file
        blocks = [b.strip() for b in content.split("=" * 60) if b.strip()]
        # Each block: timestamp, then a JSON header, then text
        parsed = []
        for b in blocks:
            # Find the JSON block (starts with {)
            m = re.search(r"\{.*?\}", b, re.DOTALL)
            if not m:
                continue
            try:
                meta = json.loads(m.group(0))
                # Find the alert text — first line that has the verdict
                lines = b.split("\n")
                text_lines = []
                capture = False
                for line in lines:
                    if line.startswith("🟢") or line.startswith("🟡") or line.startswith("🔴"):
                        capture = True
                    if capture:
                        text_lines.append(line)
                parsed.append((meta, "\n".join(text_lines).strip()[:300]))
            except json.JSONDecodeError:
                continue

        if not parsed:
            return "📜 <b>No alerts parsed from log.</b>"

        last = parsed[-n:]
        lines = [f"📜 <b>Last {len(last)} alerts</b>\n"]
        for meta, text in reversed(last):
            tier_emoji = {"A": "🟢", "B": "🟡", "C": "🔴"}.get(meta.get("tier", "?"), "⚪")
            sym = meta.get("symbol", "?")
            score = meta.get("score", 0)
            strat = meta.get("strategy", "?")
            addr = meta.get("address", "")[:8]
            lines.append(
                f"{tier_emoji} <b>${sym}</b> "
                f"<code>{addr}...</code> "
                f"| Score {score:.0f} | {strat}\n"
                f"  <i>{text.split(chr(10))[0]}</i>"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # /scan — force a scan cycle
    # ------------------------------------------------------------------
    async def cmd_scan(args: str) -> str:
        result = await agent.force_scan()
        return (
            f"🔄 <b>Scan triggered</b>\n\n"
            f"Runners found: {result['runners_found']}\n"
            f"Alerts sent today: {result['alerts_sent']}/{agent.daily_cap}\n"
            f"Open positions: {agent.ledger.stats()['open']}"
        )

    # ------------------------------------------------------------------
    # /setcap N — change daily alert cap
    # ------------------------------------------------------------------
    async def cmd_setcap(args: str) -> str:
        m = re.match(r"^\s*(\d+)\s*$", args)
        if not m:
            return "⚠️ Usage: <code>/setcap N</code> (where N is 1-200)"
        new_cap = int(m.group(1))
        if new_cap < 1 or new_cap > 200:
            return "⚠️ Cap must be between 1 and 200."
        old = agent.daily_cap
        agent.daily_cap = new_cap
        # Also persist to config.json so it survives restarts
        try:
            cfg_path = PROJECT_ROOT / "config.json"
            cfg = json.loads(cfg_path.read_text())
            cfg["scanning"]["daily_alert_cap"] = new_cap
            cfg_path.write_text(json.dumps(cfg, indent=2))
        except Exception as e:
            log.error(f"Could not persist daily_alert_cap: {e}")
        return (
            f"⚙️ <b>Daily alert cap updated</b>\n\n"
            f"Old: {old}\n"
            f"New: <b>{new_cap}</b>\n"
            f"<i>(persisted to config.json)</i>"
        )

    # ------------------------------------------------------------------
    # /closeall — close all open paper positions
    # ------------------------------------------------------------------
    async def cmd_closeall(args: str) -> str:
        positions = [p for p in agent.ledger.positions if p.status == "open"]
        if not positions:
            return "📭 <b>No open positions to close.</b>"

        # Mark all as manually closed at current (last known) price
        # We don't have live prices here without an API call;
        # use entry price as fallback so the ledger records a clean close.
        for p in positions:
            agent.ledger.close_position(p.address, p.entry_price_usd, reason="manual_closeall")
        return (
            f"🗑️ <b>Closed {len(positions)} positions</b>\n\n"
            f"Marked as manually closed in the paper ledger.\n"
            f"Run /positions to confirm (should show 0 open)."
        )

    # ------------------------------------------------------------------
    # /pause, /resume
    # ------------------------------------------------------------------
    async def cmd_pause(args: str) -> str:
        if alerter.is_paused():
            return "⏸️ Already paused."
        alerter.pause()
        return (
            "⏸️ <b>Alerts paused.</b>\n"
            "Bot keeps running, just stops sending alerts.\n"
            "/resume to start again."
        )

    async def cmd_resume(args: str) -> str:
        if not alerter.is_paused():
            return "▶️ Already running."
        alerter.resume()
        return "▶️ <b>Alerts resumed.</b>"

    # Register all commands
    alerter.register_command("help", cmd_help)
    alerter.register_command("stats", cmd_stats)
    alerter.register_command("status", cmd_status)
    alerter.register_command("positions", cmd_positions)
    alerter.register_command("wallets", cmd_wallets)
    alerter.register_command("alerts", cmd_alerts)
    alerter.register_command("scan", cmd_scan)
    alerter.register_command("setcap", cmd_setcap)
    alerter.register_command("closeall", cmd_closeall)
    alerter.register_command("pause", cmd_pause)
    alerter.register_command("resume", cmd_resume)
