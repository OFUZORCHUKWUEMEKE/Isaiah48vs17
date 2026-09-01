"""
Main agent loop - the heart of the memecoin runner agent.

Every 30s:
  1. Poll DexScreener for Solana runners
  2. Enrich with holder data (Birdeye)
  3. Apply rulebook
  4. Check if tracked wallets are buying
  5. Alert on Tier A/B verdicts
  6. Track paper positions
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from typing import Any, Dict, List, Set

from src.alerts.telegram import TelegramAlerter
from src.agent.ledger import PaperLedger
from src.data.birdeye import Birdeye
from src.data.dexscreener import DexScreener
from src.data.helius import Helius
from src.data.pumpfun import PumpFun
from src.rules.engine import RuleEngine, Verdict, Tier
from src.utils.config import load_config, has_real_credentials
from src.utils.logger import get_logger

log = get_logger("agent")


class MemecoinAgent:
    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        creds = has_real_credentials(config)
        env = config.get("_env", {})

        self.dex = DexScreener(mock_mode=False)  # DexScreener free, always real
        self.helius = Helius(api_key=env["helius_api_key"], mock_mode=not creds["helius"])
        self.birdeye = Birdeye(api_key=env["birdeye_api_key"], mock_mode=not creds["birdeye"])
        self.pumpfun = PumpFun(mock_mode=False)
        self.alerter = TelegramAlerter(
            bot_token=env["telegram_bot_token"],
            chat_id=env["telegram_chat_id"],
        )
        self.engine = RuleEngine(config["rules"])
        self.ledger = PaperLedger(
            starting_capital=10_000.0,
            position_pct=config["rules"].get("portfolio_risk", {}).get("max_position_pct_of_capital", 5.0),
        )
        # Wallet scorer — assigns Tier 1/2/3 based on win rate + ROI + recency
        from src.agent.wallet_scorer import WalletScorer
        self.scorer = WalletScorer(self.helius)

        self.tracked_wallets: List[str] = config.get("tracked_wallets", [])
        self.scan_interval = config["scanning"]["scan_interval_seconds"]
        self.wallet_poll_interval = config["scanning"]["wallet_poll_interval_seconds"]
        self.daily_cap = config["scanning"]["daily_alert_cap"]

        # Cache: which wallets we already checked recently (to avoid 429s on rapid restarts)
        self._wallet_cache: Dict[str, List[Dict]] = {}
        self._wallet_cache_time: Dict[str, float] = {}

        self._seen_addresses: Set[str] = set()
        self._last_scan_at: float = 0.0
        self._last_wallet_check = 0
        self._recent_wallet_buys: Dict[str, str] = {}  # mint -> wallet
        # Track per-tier buys: mint -> set of tier labels
        self._recent_buys_by_tier: Dict[str, set] = {}  # mint -> {"T1", "T2", "T3"}

        log.info(f"Agent initialized | mode={config['mode']} | "
                 f"real_creds={creds} | wallets={len(self.tracked_wallets)}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self):
        log.info("=" * 60)
        log.info("MEMECOIN RUNNER AGENT - STARTING")
        log.info("=" * 60)

        # Register Telegram bot commands and start polling (in a thread)
        from src.alerts.commands import register_commands
        register_commands(self.alerter, self)
        self.alerter.start_polling()

        # Run wallet scoring pass (assigns Tier 1/2/3 to each wallet)
        try:
            summary = await self.score_wallets(force=False)
            breakdown = summary.get("tier_breakdown", {})
            top = summary.get("top_wallets", [])
            top_short = ", ".join(
                f"{w['address'][:6]}..(T{w['tier']},{w['score']:.0f})"
                for w in top[:5]
            )
            log.info(
                f"Wallet scoring done. T1={breakdown.get(1, 0)} "
                f"T2={breakdown.get(2, 0)} T3={breakdown.get(3, 0)}"
            )
        except Exception as e:
            log.error(f"Initial wallet scoring failed: {e}")
            breakdown = {1: 0, 2: 0, 3: 0}
            top_short = "n/a"

        await self.alerter.send_text(
            "🤖 <b>Memecoin Runner Agent started</b>\n"
            f"Mode: {self.cfg['mode']}\n"
            f"Scan interval: {self.scan_interval}s\n"
            f"Daily alert cap: {self.daily_cap}\n"
            f"Tracked wallets: {len(self.tracked_wallets)}\n"
            f"<b>Tiers:</b> 🥇{breakdown.get(1, 0)} 🥈{breakdown.get(2, 0)} 🥉{breakdown.get(3, 0)}\n"
            f"<b>Top 5:</b> {top_short}\n"
            f"💡 Try /help for commands"
        )

        # Start daily summary scheduler (UTC time from config)
        summary_utc = self.cfg.get("alerts", {}).get("daily_summary_utc", "23:55")
        summary_task = asyncio.create_task(self._daily_summary_loop(summary_utc))
        log.info(f"Daily summary scheduled for {summary_utc} UTC")

        # Start the health/HTTP server so Railway (or any host) can ping us
        try:
            from src.agent.health_server import set_agent, start_health_server
            set_agent(self)
            port = int(os.getenv("PORT", "8080"))
            start_health_server(port=port)
            log.info(f"Health server started on port {port}")
        except Exception as e:
            log.warning(f"Could not start health server: {e}")

        # Handle SIGTERM (Railway/Render sends this on restart) for graceful shutdown
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            log.info("Received SIGTERM/SIGINT, initiating graceful shutdown")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            while not stop_event.is_set():
                self._last_scan_at = time.time()
                await self._scan_cycle()
                # Sleep with cancellation awareness
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.scan_interval)
                    break  # stop event was set during sleep
                except asyncio.TimeoutError:
                    pass  # normal cycle, continue
        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)...")
        finally:
            summary_task.cancel()
            await self._shutdown()

    async def force_scan(self) -> Dict[str, Any]:
        """Run a single scan cycle on demand (for /scan command)."""
        log.info("Force scan triggered by user")
        await self._scan_cycle()
        return {
            "triggered_at": time.time(),
            "runners_found": len(getattr(self, "_last_runners", [])),
            "alerts_sent": self.alerter._today_count,
        }

    async def score_wallets(self, force: bool = False) -> Dict[str, Any]:
        """Run a full scoring pass on all tracked wallets.
        Returns summary: {scored, skipped_cached, tier_breakdown, top_wallets}.
        """
        if not force and self.scorer.is_cache_fresh():
            log.info("Wallet scores are fresh, skipping re-score")
            return {
                "scored": 0,
                "skipped_cached": len(self.tracked_wallets),
                "tier_breakdown": self.scorer.get_tier_breakdown(),
                "top_wallets": [
                    {"address": w, "score": s, "tier": t}
                    for w, s, t in self.scorer.get_top_wallets(10)
                ],
            }
        log.info(f"Starting wallet scoring pass for {len(self.tracked_wallets)} wallets...")
        await self.scorer.score_all(self.tracked_wallets, force=force)
        breakdown = self.scorer.get_tier_breakdown()
        top = self.scorer.get_top_wallets(10)
        log.info(f"Scoring complete. Tier breakdown: {breakdown}")
        return {
            "scored": len(self.tracked_wallets),
            "skipped_cached": 0,
            "tier_breakdown": breakdown,
            "top_wallets": [
                {"address": w, "score": s, "tier": t}
                for w, s, t in top
            ],
        }

    async def _daily_summary_loop(self, utc_time: str):
        """Background task that posts a daily summary at the given UTC time.
        Format: 'HH:MM' (24h). Runs once per day, aligned to UTC.
        """
        try:
            m = re.match(r"^(\d{1,2}):(\d{2})$", utc_time.strip())
            if not m:
                log.warning(f"Invalid daily_summary_utc '{utc_time}', defaulting to 23:55")
                target_h, target_m = 23, 55
            else:
                target_h, target_m = int(m.group(1)), int(m.group(2))
        except Exception:
            target_h, target_m = 23, 55

        sent_today_for = None  # date we last sent for
        while True:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                if sent_today_for != now.date():
                    if now.hour > target_h or (now.hour == target_h and now.minute >= target_m):
                        # Time to send
                        await self._send_daily_summary()
                        sent_today_for = now.date()
                # Sleep ~60s and recheck
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Daily summary error: {e}")
                await asyncio.sleep(60)

    async def _send_daily_summary(self):
        """Build and send the daily summary message via the main async alerter."""
        stats = self.ledger.stats()
        recent_buys = len(self._recent_wallet_buys)
        msg = (
            f"📅 <b>Daily Summary</b>\n\n"
            f"📨 Alerts sent: <b>{self.alerter._today_count}</b>\n"
            f"🐋 Recent wallet buys (10m): <b>{recent_buys}</b>\n\n"
            f"💰 <b>Paper P&amp;L</b>\n"
            f"Open: {stats['open']} | Closed: {stats['closed']}\n"
            f"Win rate: <b>{stats['win_rate']:.1f}%</b>\n"
            f"Total P&amp;L: <b>${stats['total_pnl_usd']:+,.2f}</b>\n"
            f"Avg win: {stats['avg_win_pct']:+.1f}% | "
            f"Avg loss: {stats['avg_loss_pct']:+.1f}%\n\n"
            f"<i>Type /stats or /positions for details.</i>"
        )
        try:
            await self.alerter.send_text(msg)
            log.info("Daily summary sent")
        except Exception as e:
            log.error(f"Daily summary send failed: {e}")

    async def _scan_cycle(self):
        """One full scan iteration."""
        try:
            # 1. Get runner candidates from DexScreener
            runners = await self.dex.get_solana_runners(
                min_volume_5m=30_000,
                limit=self.cfg["scanning"]["dexscreener_search_limit"],
            )
            log.info(f"Found {len(runners)} Solana runners")

            # 2. Get pre-migration candidates from pump.fun
            pf_coins = await self.pumpfun.get_coins_by_criteria()
            if pf_coins:
                log.info(f"Found {len(pf_coins)} pump.fun pre-migration candidates")

            # 3. Check tracked wallets periodically
            now = time.time()
            if now - self._last_wallet_check > self.wallet_poll_interval:
                await self._check_tracked_wallets()
                self._last_wallet_check = now

            # 4. Evaluate all candidates
            all_candidates = runners + pf_coins
            verdicts: List[Verdict] = []
            momentum_rejects = 0
            for token in all_candidates:
                if not token or not token.get("address"):
                    continue
                # Enrich with holder data
                if "top10_pct" not in token:
                    token = await self.birdeye.enrich_token(token)
                # Add mock pro-trader data if not present
                token.setdefault("pro_traders", 50)
                token.setdefault("fib_retracement", 0)
                # Apply momentum filter first (video 2.5: only active coins)
                passes_mom, mom_failures = self.engine.passes_momentum_filter(token)
                if not passes_mom:
                    momentum_rejects += 1
                    continue
                # Apply rules
                verdict = self.engine.evaluate(token)
                # Upgrade tier based on WHICH tier of wallet(s) is buying.
                # Tier 1 = strong Tier A boost (top 10% wallets)
                # Tier 2 = moderate boost
                # Tier 3 = light boost (current default behavior)
                token_addr = token.get("address", "")
                if token_addr in self._recent_buys_by_tier:
                    tiers_bought = self._recent_buys_by_tier[token_addr]
                    # Pick the BEST wallet that bought this token
                    best_wallet = None
                    best_tier = 3
                    for mint, wallet in self._recent_wallet_buys.items():
                        if mint == token_addr:
                            t = self.scorer.get_tier(wallet)
                            if t < best_tier:
                                best_tier = t
                                best_wallet = wallet
                    if best_wallet:
                        verdict = self.engine.apply_wallet_signal(verdict, best_wallet, tier=best_tier)
                        verdict.data["wallet_tiers"] = sorted(tiers_bought)
                verdicts.append(verdict)

            # 5. Sort by tier then score
            verdicts.sort(key=lambda v: (v.tier.value, v.score), reverse=True)

            # 6. Send alerts
            sent = 0
            for v in verdicts:
                if v.tier == Tier.C:
                    continue
                if v.token_address in self._seen_addresses:
                    continue
                ok = await self.alerter.send_verdict(v, daily_cap=self.daily_cap)
                if ok:
                    self._seen_addresses.add(v.token_address)
                    sent += 1
                    # Open paper position if Tier A
                    if v.tier == Tier.A and v.passed:
                        self.ledger.open_position(
                            symbol=v.symbol, address=v.token_address,
                            strategy=v.strategy or "unknown",
                            mcap_usd=v.data.get("mcap_usd", 0),
                            price_usd=v.data.get("price_usd", 0),
                            tier=v.tier.value, score=v.score,
                        )
                    if sent >= 10:  # per-cycle cap
                        break

            # 7. Update paper positions
            current_mcaps = {v.token_address: v.data.get("mcap_usd", 0) for v in verdicts}
            self.ledger.update_open_positions(current_mcaps)

            log.info(f"Cycle complete | {len(verdicts)} evaluated | {sent} alerts sent | "
                     f"open positions={self.ledger.stats()['open']}")

        except Exception as e:
            log.error(f"Scan cycle error: {e}", exc_info=True)

    async def _check_tracked_wallets(self):
        """Check tracked wallets for recent token buys.

        With 200+ wallets and Helius free-tier limits (~10 req/sec),
        we process serially with 0.12s between calls, and cache results
        for wallet_poll_interval seconds.
        """
        valid_wallets = [
            w for w in self.tracked_wallets
            if "MOCK" not in w and "REPLACE" not in w
        ]
        if not valid_wallets:
            log.debug("No real tracked wallets - skipping")
            return

        now = time.time()
        cache_ttl = self.wallet_poll_interval

        # Determine which wallets need fresh fetch
        wallets_to_fetch = [
            w for w in valid_wallets
            if w not in self._wallet_cache_time
            or (now - self._wallet_cache_time[w]) > cache_ttl
        ]
        if not wallets_to_fetch:
            self._rebuild_recent_buys()
            log.debug(f"All {len(valid_wallets)} wallets cached. "
                      f"Recent buys: {len(self._recent_wallet_buys)}")
            return

        log.info(f"Fetching {len(wallets_to_fetch)}/{len(valid_wallets)} wallets (rest cached)...")
        self._recent_wallet_buys.clear()

        # Serial fetching with delay - safest for rate limit
        # 0.12s between calls = ~8 req/sec, under the 10/sec limit
        for wallet in wallets_to_fetch:
            await self._fetch_wallet_safe(wallet)
            await asyncio.sleep(0.12)

        self._rebuild_recent_buys()
        log.info(f"Wallet check complete: {len(self._recent_wallet_buys)} recent buys from {len(valid_wallets)} wallets")

    async def _fetch_wallet_safe(self, wallet: str):
        """Fetch a wallet's txs and cache the result. Silently handle errors.
        Pulls 100 txs (vs the previous 20) so we have enough history to
        score the wallet's win rate / ROI on each cycle.
        """
        try:
            txs = await self.helius.get_wallet_transactions(wallet, limit=100)
            self._wallet_cache[wallet] = txs
            self._wallet_cache_time[wallet] = time.time()
        except Exception as e:
            log.debug(f"Wallet {wallet[:8]} fetch failed: {e}")
            # Don't update cache on failure - we'll retry next time.
            # This only works because the client raises on failure; when it
            # returned [] instead, a failed fetch was cached as "no buys" and
            # the wallet's signal went silently missing until the TTL expired.

    def _rebuild_recent_buys(self):
        """Build the mint -> wallet map and per-tier buy map from cached transactions."""
        now = time.time()
        lookback = self.cfg.get("rules", {}).get("wallet_signals", {}).get("wallet_lookback_minutes", 10) * 60
        cutoff = now - lookback
        # mint -> first buyer wallet (kept for backward compat)
        # mint -> set of tier labels that have bought
        # mint -> list of (wallet, tier) tuples for the alert
        self._recent_wallet_buys.clear()
        self._recent_buys_by_tier.clear()

        for wallet, txs in self._wallet_cache.items():
            tier = self.scorer.get_tier(wallet)
            tier_label = f"T{tier}"
            for tx in txs:
                ts = tx.get("timestamp", 0)
                if ts < cutoff:
                    continue
                if tx.get("type") in ("SWAP", "BUY") or "swap" in str(tx.get("description", "")).lower():
                    token_changes = tx.get("tokenTransfers", []) or []
                    for change in token_changes:
                        if change.get("toUserAccount") == wallet:
                            mint = change.get("mint")
                            if mint:
                                # First buyer (for backward compat)
                                if mint not in self._recent_wallet_buys:
                                    self._recent_wallet_buys[mint] = wallet
                                # Track tier presence
                                if mint not in self._recent_buys_by_tier:
                                    self._recent_buys_by_tier[mint] = set()
                                self._recent_buys_by_tier[mint].add(tier_label)

    async def _shutdown(self):
        stats = self.ledger.stats()
        log.info(f"Final stats: {stats}")
        # Send stop message BEFORE closing the alerter
        try:
            await self.alerter.send_text(
                f"🛑 <b>Agent stopped</b>\n"
                f"Open: {stats['open']} | Closed: {stats['closed']} | "
                f"Win rate: {stats['win_rate']:.1f}%"
            )
        except Exception as e:
            log.error(f"Final Telegram send failed: {e}")
        # Then close everything
        await self.dex.close()
        await self.helius.close()
        await self.birdeye.close()
        await self.pumpfun.close()
        await self.alerter.close()


async def main():
    cfg = load_config()
    agent = MemecoinAgent(cfg)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
