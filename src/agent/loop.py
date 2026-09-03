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
from typing import Any, Dict, List, Set, Tuple

from src.alerts.telegram import TelegramAlerter
from src.agent.ledger import PaperLedger
from src.data.birdeye import Birdeye
from src.data.dexscreener import DexScreener
from src.data.gmgn import GMGNClient
from src.data.helius import Helius
from src.data.pumpfun import PumpFun
from src.rules.engine import RuleEngine, Verdict, Tier
from src.rules.indicators import volume_metrics, fib_retracement
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
        gmgn_cfg = config.get("gmgn", {})
        self.gmgn = GMGNClient(
            api_key=env.get("gmgn_api_key", ""),
            base_url=env.get("gmgn_base_url", ""),
            chain=gmgn_cfg.get("chain", "sol"),
            mock_mode=not creds["gmgn"],
            transport=gmgn_cfg.get("transport", "auto"),
        )
        # Stage 2 of docs/gmgn-integration-plan.md: enrichment only, opt-in.
        # Requires BOTH the config flag and a real key - gmgn.enabled=true
        # with no key would otherwise silently run GMGN's own mock data
        # through the live pipeline instead of falling back to Birdeye.
        self.gmgn_enabled = bool(gmgn_cfg.get("enabled", False)) and creds["gmgn"]
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
        self._tracked_wallets_set = set(self.tracked_wallets)
        self.scan_interval = config["scanning"]["scan_interval_seconds"]
        self.wallet_poll_interval = config["scanning"]["wallet_poll_interval_seconds"]
        self.daily_cap = config["scanning"]["daily_alert_cap"]

        # Cache: which wallets we already checked recently (to avoid 429s on rapid restarts)
        self._wallet_cache: Dict[str, List[Dict]] = {}
        self._wallet_cache_time: Dict[str, float] = {}

        # Cache: K-line candles per token address (GMGN only, stage 4).
        # TTL'd separately from wallet caching - see _attach_kline_indicators.
        self._kline_cache: Dict[str, List[Dict]] = {}
        self._kline_cache_time: Dict[str, float] = {}

        self._seen_addresses: Set[str] = set()
        self._last_scan_at: float = 0.0
        self._last_wallet_check = 0
        # mint -> [(wallet, tier), ...]. Was mint -> a single first-buyer
        # wallet; that meant _evaluate_candidates's "pick the best wallet"
        # search never had more than one candidate - a no-op. See the
        # "prerequisite fix" note in docs/gmgn-integration-plan.md #5,
        # needed before a second signal source (the GMGN wallet feed,
        # stage 5) could contribute anything real.
        self._recent_wallet_buys: Dict[str, List[Tuple[str, int]]] = {}
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

    async def _enrich(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Add holder/insider/bundle data to a token dict.

        GMGN is used only when gmgn.enabled is set in config AND a real
        API key is configured (self.gmgn_enabled, computed in __init__).
        Otherwise Birdeye is used, unchanged from before this method
        existed - so with the shipped default (gmgn.enabled: false) this
        is exactly the old `await self.birdeye.enrich_token(token)` call.

        Both cmd_once (bot.py) and _scan_cycle call this so there is one
        place that decides which source enriches a token, rather than two
        copies of the same branch that can drift out of sync.
        """
        if self.gmgn_enabled:
            return await self.gmgn.enrich_token(token)
        return await self.birdeye.enrich_token(token)

    async def _discover_runners(self) -> List[Dict[str, Any]]:
        """Get Solana runner candidates for this cycle.

        When gmgn_enabled, GMGN's /v1/market/rank is the primary source -
        one request returns up to rank_limit candidates that already carry
        holder/security/smart-money fields (GMGNClient._normalize_rank_item).
        That is what collapses the old per-token Birdeye enrichment fan-out:
        the `if "top10_pct" not in token` guard in _scan_cycle skips
        enrichment entirely for these, since it's already there. DexScreener
        is still queried and merged in via setdefault (so it can never
        clobber a GMGN field) to fill whichever price-change/txns fields
        GMGN's single --interval didn't cover, for any address both
        sources saw.

        Falls back to DexScreener alone - identical to the pre-stage-3
        behavior - when GMGN is disabled, unkeyed, or its rank call fails
        outright. A single failed discovery call should degrade the agent
        for one cycle, not crash it or leave it with zero candidates.
        """
        dex_runners = await self.dex.get_solana_runners(
            min_volume_5m=30_000,
            limit=self.cfg["scanning"]["dexscreener_search_limit"],
        )

        if not self.gmgn_enabled:
            return dex_runners

        g = self.cfg.get("gmgn", {})
        gmgn_candidates = await self.gmgn.get_discovery_candidates(
            interval=g.get("rank_interval", "5m"),
            limit=g.get("rank_limit", 100),
            order_by=g.get("rank_order_by", "volume"),
            min_liquidity=g.get("min_liquidity_usd", 20000),
            max_rug_ratio=g.get("max_rug_ratio", 0.30),
            min_created=g.get("min_created", "30m"),
        )
        if not gmgn_candidates:
            log.debug("GMGN rank returned no candidates this cycle; using DexScreener alone")
            return dex_runners

        by_address = {t["address"]: t for t in gmgn_candidates}
        dex_by_address = {t["address"]: t for t in dex_runners if t.get("address")}

        for addr, dex_token in dex_by_address.items():
            if addr in by_address:
                for k, v in dex_token.items():
                    by_address[addr].setdefault(k, v)
            else:
                by_address[addr] = dex_token

        merged = list(by_address.values())
        log.info(
            f"Discovery: {len(gmgn_candidates)} GMGN + {len(dex_runners)} DexScreener "
            f"-> {len(merged)} merged candidates"
        )
        return merged

    async def _attach_kline_indicators(self, survivors: List[Dict[str, Any]]) -> None:
        """Fetch real K-line data for tokens that survived the momentum
        filter and attach volume_1m_usd/volume_15m_usd/volume_spike_ratio/
        fib_retracement, mutating each token dict in place. This is what
        actually fixes volume_spike_ratio being algebraically stuck at 5.0
        (dexscreener.py's synthetic derivation) and fib_retracement being a
        permanent 0 - see src/rules/indicators.py and
        docs/gmgn-integration-plan.md #3-4.

        No-op when gmgn_enabled is False, mirroring _enrich()'s and
        _discover_runners()'s self-contained gating - callers don't need
        to check the flag themselves.

        Budget-limited: klines are the one remaining per-token GMGN call
        (weight 2), so only up to kline_budget_per_cycle *new* fetches
        happen per call - see the plan's "K-line budget" note. A cache hit
        is free and doesn't count against the budget; once the budget is
        exhausted, tokens without a fresh cache entry are simply skipped
        for this call rather than the whole pass aborting.

        volume_5m_usd is left alone (filled only via setdefault, never
        overwritten) - it's already populated by discovery (GMGN rank or
        DexScreener), and comparing kline-derived volume_1m_usd against
        that already-trusted figure in check_volume_decay is more
        consistent than replacing it with an independently-windowed sum
        that could disagree with what discovery reported.
        """
        if not self.gmgn_enabled:
            return

        g = self.cfg.get("gmgn", {})
        budget = g.get("kline_budget_per_cycle", 8)
        resolution = g.get("kline_resolution", "1m")
        lookback = g.get("kline_lookback_candles", 60)
        fib_lookback = g.get("fib_lookback_candles", 288)
        ttl = self.scan_interval

        now = time.time()
        fetched = 0
        for token in survivors:
            addr = token.get("address", "")
            if not addr:
                continue

            cached = self._kline_cache.get(addr)
            fresh = cached is not None and (now - self._kline_cache_time.get(addr, 0)) < ttl
            if fresh:
                candles = cached
            elif fetched < budget:
                candles = await self.gmgn.get_kline(addr, resolution=resolution)
                self._kline_cache[addr] = candles
                self._kline_cache_time[addr] = now
                fetched += 1
            else:
                continue  # budget exhausted this call, no fresh cache - leave the token as-is

            if not candles:
                continue

            vm = volume_metrics(candles, lookback=lookback)
            token["volume_1m_usd"] = vm["volume_1m_usd"]
            token["volume_15m_usd"] = vm["volume_15m_usd"]
            token["volume_spike_ratio"] = vm["volume_spike_ratio"]
            token.setdefault("volume_5m_usd", vm["volume_5m_usd"])
            token["fib_retracement"] = fib_retracement(candles, lookback=fib_lookback)

    async def _evaluate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Verdict]:
        """Turn raw discovery candidates into Verdicts: enrich, apply the
        momentum filter, attach real K-line indicators for the survivors
        (GMGN only), run the rule engine, and apply any tracked-wallet
        tier boost.

        Both _scan_cycle and bot.py's cmd_once call this - a single place
        that decides how a candidate becomes a verdict, rather than two
        copies of the same steps that can drift out of sync. That drift is
        not hypothetical: cmd_once had to be fixed to match _scan_cycle
        after both stage 2 (PR #5, the enrichment call) and stage 3
        (PR #6, the discovery call and a missing enrichment guard) each
        found it out of date in a different way.
        """
        survivors: List[Dict[str, Any]] = []
        momentum_rejects = 0
        for token in candidates:
            if not token or not token.get("address"):
                continue
            # Enrich with holder data (GMGN if enabled+keyed, else Birdeye)
            if "top10_pct" not in token:
                token = await self._enrich(token)
            # Add mock pro-trader data if not present
            token.setdefault("pro_traders", 50)
            token.setdefault("fib_retracement", 0)
            # Apply momentum filter first (video 2.5: only active coins)
            passes_mom, mom_failures = self.engine.passes_momentum_filter(token)
            if not passes_mom:
                momentum_rejects += 1
                continue
            survivors.append(token)
        if momentum_rejects:
            log.debug(f"Momentum filter rejected {momentum_rejects} candidate(s)")

        # Real K-line indicators for the survivors (GMGN only; no-op otherwise)
        await self._attach_kline_indicators(survivors)

        verdicts: List[Verdict] = []
        for token in survivors:
            verdict = self.engine.evaluate(token)
            # Upgrade tier based on WHICH tier of wallet(s) is buying.
            # Tier 1 = strong Tier A boost (top 10% wallets)
            # Tier 2 = moderate boost
            # Tier 3 = light boost (current default behavior)
            token_addr = token.get("address", "")
            # Pick the BEST (lowest-numbered) wallet tier among everyone who
            # bought this token, curated or GMGN-feed-sourced. The tier is
            # read from the (wallet, tier) tuple captured when the buy was
            # recorded (_rebuild_recent_buys / _check_gmgn_wallet_feed) -
            # NOT re-derived here via scorer.get_tier(), because that would
            # forget a feed-only wallet's configured feed_wallet_tier and
            # silently fall back to the scorer's own (currently identical,
            # but unrelated) default for unscored wallets.
            buyers = self._recent_wallet_buys.get(token_addr)
            if buyers:
                best_wallet, best_tier = min(buyers, key=lambda wt: wt[1])
                tiers_bought = self._recent_buys_by_tier.get(token_addr, set())
                verdict = self.engine.apply_wallet_signal(verdict, best_wallet, tier=best_tier)
                verdict.data["wallet_tiers"] = sorted(tiers_bought)
            verdicts.append(verdict)
        return verdicts

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
            # 1. Get runner candidates (GMGN primary when enabled, merged
            # with DexScreener; DexScreener alone otherwise - see _discover_runners)
            runners = await self._discover_runners()
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

            # 4. Evaluate all candidates (enrich, momentum-filter, attach
            # real K-line indicators for survivors, apply rules, wallet-tier
            # boost - see _evaluate_candidates)
            all_candidates = runners + pf_coins
            verdicts = await self._evaluate_candidates(all_candidates)

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

        Ends by folding in the GMGN smart-money/KOL feed (stage 5) on top
        of whatever the curated-wallet rebuild below produced - see
        _check_gmgn_wallet_feed, which no-ops when gmgn_enabled is False.
        That call happens on every exit path (no valid wallets, all
        cached, or a fresh fetch) so the feed still gets polled on its own
        schedule even when there's nothing for Helius to do this cycle.
        """
        valid_wallets = [
            w for w in self.tracked_wallets
            if "MOCK" not in w and "REPLACE" not in w
        ]
        if not valid_wallets:
            log.debug("No real tracked wallets - skipping Helius poll")
            await self._check_gmgn_wallet_feed()
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
            await self._check_gmgn_wallet_feed()
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
        await self._check_gmgn_wallet_feed()
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
        """Build the mint -> [(wallet, tier), ...] map and per-tier buy set
        from cached Helius transactions for the curated tracked-wallet list.

        Clears and fully rebuilds both structures from self._wallet_cache
        every call - _check_gmgn_wallet_feed runs after this (never
        before) and only adds on top, so the two sources compose into one
        coherent window each poll rather than one wiping the other.
        """
        now = time.time()
        lookback = self.cfg.get("rules", {}).get("wallet_signals", {}).get("wallet_lookback_minutes", 10) * 60
        cutoff = now - lookback
        self._recent_wallet_buys.clear()
        self._recent_buys_by_tier.clear()

        for wallet, txs in self._wallet_cache.items():
            tier = self.scorer.get_tier(wallet)
            tier_label = f"T{tier}"
            seen_mints_this_wallet = set()
            for tx in txs:
                ts = tx.get("timestamp", 0)
                if ts < cutoff:
                    continue
                if tx.get("type") in ("SWAP", "BUY") or "swap" in str(tx.get("description", "")).lower():
                    token_changes = tx.get("tokenTransfers", []) or []
                    for change in token_changes:
                        if change.get("toUserAccount") == wallet:
                            mint = change.get("mint")
                            if mint and mint not in seen_mints_this_wallet:
                                seen_mints_this_wallet.add(mint)
                                self._recent_wallet_buys.setdefault(mint, []).append((wallet, tier))
                                self._recent_buys_by_tier.setdefault(mint, set()).add(tier_label)

    async def _check_gmgn_wallet_feed(self):
        """Poll GMGN's smartmoney + KOL feeds and fold their buys into
        _recent_wallet_buys / _recent_buys_by_tier on top of whatever
        _rebuild_recent_buys() just built - does NOT clear those
        structures, only adds to them (see that method's docstring for
        why the ordering matters).

        GMGN can't answer "which wallets bought token X" directly; it
        exposes platform-wide feeds of recent trades by algorithmically-
        identified smart money and tagged KOLs instead (see
        docs/gmgn-integration-plan.md #5), so the mint -> wallet(s) map is
        built by inverting them: keep only "buy" trades at or above
        min_wallet_buy_usd within the lookback window, the same filters
        _rebuild_recent_buys applies to Helius data and read from the
        same rules.wallet_signals config block. side/min_amount_usd are
        also passed to the feed calls as best-effort server-side filters
        (unverified against a live API, same caveat as every other GMGN
        route - see src/data/gmgn.py's module docstring); the client-side
        filtering here is the actual source of truth regardless of
        whether the server honors them.

        A feed wallet that is ALSO on the curated tracked_wallets list
        keeps its real WalletScorer-assigned tier, via the same
        scorer.get_tier() lookup _rebuild_recent_buys uses. A feed-only
        wallet gets gmgn.feed_wallet_tier (default 3) instead of relying
        on WalletScorer's own default-to-3 behavior for unscored wallets,
        because that default is incidental - it wouldn't track a
        deliberately-configured feed_wallet_tier if someone changed it.
        Deliberately tier 3, not something that could reach tier 1/2:
        apply_wallet_signal only force-promotes tiers 1-2 to Tier A, and
        mapping the whole platform-wide feed there would flood Tier A
        alerts and drown out the curated list's entire point.

        No-op when gmgn_enabled is False, matching every other GMGN-gated
        method's self-contained gating (_enrich, _discover_runners,
        _attach_kline_indicators).
        """
        if not self.gmgn_enabled:
            return

        wallet_cfg = self.cfg.get("rules", {}).get("wallet_signals", {})
        min_buy_usd = wallet_cfg.get("min_wallet_buy_usd", 100)
        lookback_s = wallet_cfg.get("wallet_lookback_minutes", 10) * 60
        cutoff = time.time() - lookback_s
        feed_tier = self.cfg.get("gmgn", {}).get("feed_wallet_tier", 3)

        try:
            smart, kol = await asyncio.gather(
                self.gmgn.get_smartmoney_feed(side="buy", min_amount_usd=min_buy_usd),
                self.gmgn.get_kol_feed(side="buy", min_amount_usd=min_buy_usd),
            )
        except Exception as e:
            log.debug(f"GMGN wallet feed fetch failed: {e}")
            return

        added = 0
        for entry in (smart or []) + (kol or []):
            if entry.get("side") != "buy":
                continue
            if (entry.get("amount_usd") or 0) < min_buy_usd:
                continue
            ts = entry.get("timestamp", 0)
            if ts < cutoff:
                continue
            mint = entry.get("base_address") or ""
            wallet = entry.get("maker") or ""
            if not mint or not wallet:
                continue

            tier = self.scorer.get_tier(wallet) if wallet in self._tracked_wallets_set else feed_tier
            tier_label = f"T{tier}"

            existing = self._recent_wallet_buys.setdefault(mint, [])
            if not any(w == wallet for w, _ in existing):
                existing.append((wallet, tier))
                added += 1
            self._recent_buys_by_tier.setdefault(mint, set()).add(tier_label)

        if added:
            log.info(f"GMGN wallet feed: {added} new buy signal(s) from smartmoney+KOL feeds")

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
        await self.gmgn.close()
        await self.alerter.close()


async def main():
    cfg = load_config()
    agent = MemecoinAgent(cfg)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
