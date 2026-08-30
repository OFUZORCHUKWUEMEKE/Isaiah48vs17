"""
DexScreener API client.

Docs: https://docs.dexscreener.com/
Free tier: ~60 req/min, no key required.

Provides:
  - search_runners(): find high-volume Solana pairs
  - get_token(): detailed info on a specific token
  - get_pairs_by_address(): get all pairs for a token
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from src.utils.logger import get_logger

log = get_logger("dexscreener")

BASE_URL = "https://api.dexscreener.com/latest/dex"
RATE_LIMIT_DELAY = 1.1  # seconds between calls (be polite)


class DexScreener:
    def __init__(self, mock_mode: bool = False):
        self.mock_mode = mock_mode
        self._last_call = 0.0
        self._client = httpx.AsyncClient(timeout=15.0, headers={
            "User-Agent": "memecoin-runner-agent/0.1.0"
        })

    async def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < RATE_LIMIT_DELAY:
            await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_call = time.time()

    async def search_pairs(self, query: str = "solana") -> List[Dict[str, Any]]:
        """Search for Solana pairs. Returns raw pair dicts."""
        if self.mock_mode:
            return self._mock_pairs()

        await self._throttle()
        try:
            r = await self._client.get(f"{BASE_URL}/search", params={"q": query})
            r.raise_for_status()
            data = r.json()
            return data.get("pairs", [])
        except Exception as e:
            log.error(f"DexScreener search failed: {e}")
            return []

    async def get_token_pairs(self, token_address: str) -> List[Dict[str, Any]]:
        """Get all pairs for a specific token address."""
        if self.mock_mode:
            return [self._mock_pair(token_address)]

        await self._throttle()
        try:
            r = await self._client.get(f"{BASE_URL}/tokens/{token_address}")
            r.raise_for_status()
            data = r.json()
            return data.get("pairs", [])
        except Exception as e:
            log.error(f"DexScreener get_token_pairs({token_address}) failed: {e}")
            return []

    async def get_solana_runners(self, min_volume_5m: float = 30000,
                                  min_mcap: float = 0,
                                  max_mcap: float = float("inf"),
                                  limit: int = 50) -> List[Dict[str, Any]]:
        """High-level: get Solana pairs that look like runners.

        Combines two strategies:
          1. Seed tokens (BONK, WIF, POPCAT, etc.) - known runners
          2. Discovery search - new pairs being detected
        """
        seed_tokens = [
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB",   # BONK
            "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
            "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",  # POPCAT
            "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",  # PNUT
            "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump",  # GOAT
            "So11111111111111111111111111111111111111112",   # SOL
            "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
        ]
        # Discovery queries - find new and emerging tokens
        discovery_queries = ["solana memecoin", "pump fun", "ai agent"]
        all_pairs = []

        for mint in seed_tokens:
            try:
                pairs = await self.get_token_pairs(mint)
                all_pairs.extend(pairs or [])
            except Exception as e:
                log.debug(f"seed {mint[:8]} failed: {e}")

        for q in discovery_queries:
            try:
                pairs = await self.search_pairs(q)
                all_pairs.extend(pairs or [])
            except Exception as e:
                log.debug(f"search '{q}' failed: {e}")

        # Dedupe by pair address
        seen = set()
        unique = []
        for p in all_pairs:
            if not p:
                continue
            pa = p.get("pairAddress")
            if pa and pa not in seen:
                seen.add(pa)
                unique.append(p)
        sol_pairs = [p for p in unique if p.get("chainId") == "solana"]

        runners = []
        for p in sol_pairs:
            v5 = float(p.get("volume", {}).get("m5", 0) or 0)
            mc = float(p.get("marketCap", 0) or 0)
            if v5 < min_volume_5m:
                continue
            if mc < min_mcap or mc > max_mcap:
                continue
            runners.append(self._normalize(p))

        # Sort by 5m volume desc
        runners.sort(key=lambda x: x.get("volume_5m_usd", 0), reverse=True)
        return runners[:limit]

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize DexScreener raw pair into our internal token schema."""
        if not raw:
            return {}
        base = raw.get("baseToken") or {}
        vol = raw.get("volume") or {}
        liq = raw.get("liquidity") or {}
        txns = raw.get("txns") or {}
        price_change = raw.get("priceChange") or {}
        info = raw.get("info") or {}

        # Age in minutes (pairCreatedAt is unix ms)
        age_min = 0
        if raw.get("pairCreatedAt"):
            try:
                created_ms = int(raw["pairCreatedAt"])
                age_min = (time.time() * 1000 - created_ms) / 60000
            except (ValueError, TypeError):
                pass

        m5 = float(vol.get("m5", 0) or 0)
        h1 = float(vol.get("h1", 0) or 0)
        h24 = float(vol.get("h24", 0) or 0)

        # Volume spike ratio: 5m vs 1m-equivalent baseline
        vol1m = m5 / 5 if m5 > 0 else 0
        spike_ratio = (m5 / vol1m) if vol1m > 0 else 0

        # Buy/sell ratios (momentum indicator)
        txns_m5 = txns.get("m5", {}) or {}
        txns_h1 = txns.get("h1", {}) or {}
        buys_m5 = int(txns_m5.get("buys", 0) or 0)
        sells_m5 = int(txns_m5.get("sells", 0) or 0)
        buys_h1 = int(txns_h1.get("buys", 0) or 0)
        sells_h1 = int(txns_h1.get("sells", 0) or 0)

        buy_sell_ratio_m5 = (buys_m5 / sells_m5) if sells_m5 > 0 else (buys_m5 if buys_m5 > 0 else 0)
        buy_sell_ratio_h1 = (buys_h1 / sells_h1) if sells_h1 > 0 else (buys_h1 if buys_h1 > 0 else 0)

        # Price change % (already in % from DexScreener)
        pc_m5 = float(price_change.get("m5", 0) or 0)
        pc_h1 = float(price_change.get("h1", 0) or 0)
        pc_h6 = float(price_change.get("h6", 0) or 0)
        pc_h24 = float(price_change.get("h24", 0) or 0)

        # FDV / MCAP ratio
        mcap = float(raw.get("marketCap", 0) or 0)
        fdv = float(raw.get("fdv", 0) or 0)
        fdv_mcap_ratio = (fdv / mcap) if mcap > 0 else 0

        # Social presence
        socials = info.get("socials", []) or []
        websites = info.get("websites", []) or []
        has_twitter = any(s.get("type") == "twitter" for s in socials)
        has_telegram = any(s.get("type") == "telegram" for s in socials)
        has_website = len(websites) > 0
        social_count = len(socials)

        return {
            "address": base.get("address", ""),
            "symbol": base.get("symbol", "???"),
            "name": base.get("name", ""),
            "chain": raw.get("chainId", "solana"),
            "dex": raw.get("dexId", ""),
            "pair_address": raw.get("pairAddress", ""),
            "price_usd": float(raw.get("priceUsd", 0) or 0),
            "mcap_usd": mcap,
            "fdv_usd": fdv,
            "liquidity_usd": float(liq.get("usd", 0) or 0),
            "volume_1m_usd": vol1m,
            "volume_5m_usd": m5,
            "volume_15m_usd": m5 * 3,
            "volume_1h_usd": h1,
            "volume_24h_usd": h24,
            "txns_5m_buys": buys_m5,
            "txns_5m_sells": sells_m5,
            "txns_1h_buys": buys_h1,
            "txns_1h_sells": sells_h1,
            "buy_sell_ratio_m5": buy_sell_ratio_m5,
            "buy_sell_ratio_h1": buy_sell_ratio_h1,
            "price_change_m5_pct": pc_m5,
            "price_change_h1_pct": pc_h1,
            "price_change_h6_pct": pc_h6,
            "price_change_h24_pct": pc_h24,
            "fdv_mcap_ratio": fdv_mcap_ratio,
            "has_twitter": has_twitter,
            "has_telegram": has_telegram,
            "has_website": has_website,
            "social_count": social_count,
            "age_minutes": age_min,
            "volume_spike_ratio": spike_ratio,
            "url": raw.get("url", ""),
            "raw": raw,
        }

    # ----- MOCK DATA (when no real API or for testing) -----
    def _mock_pairs(self) -> List[Dict[str, Any]]:
        """Mock data for testing without real API calls."""
        return [self._mock_pair(addr) for addr in [
            "MOCK1So11111111111111111111111111111111111111",
            "MOCK2EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTD",
            "MOCK3DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB",
            "MOCK4SoMock1111111111111111111111111111111111",
            "MOCK5SoMock2222222222222222222222222222222222",
        ]]

    def _mock_pair(self, address: str) -> Dict[str, Any]:
        """Single mock pair."""
        now_ms = int(time.time() * 1000)
        # Create tokens with varying conditions
        rng = hash(address) % 100
        if rng < 30:
            # Young runner (pre-migration candidate)
            mcap = 25_000 + (rng * 1000)
            v5 = 60_000 + (rng * 5000)
            top10 = 25 + rng % 15
            insider = 15 + rng % 10
        elif rng < 60:
            # Established pullback candidate
            mcap = 3_500_000 + (rng * 100_000)
            v5 = 45_000 + (rng * 3000)
            top10 = 20 + rng % 10
            insider = 8 + rng % 5
        elif rng < 80:
            # Volume spike candidate
            mcap = 800_000 + (rng * 50_000)
            v5 = 250_000 + (rng * 20_000)
            top10 = 30 + rng % 12
            insider = 12 + rng % 8
        else:
            # Rug candidate (should fail rules)
            mcap = 35_000
            v5 = 80_000
            top10 = 65
            insider = 45

        return {
            "chainId": "solana",
            "dexId": "raydium",
            "baseToken": {"address": address, "symbol": f"MOCK{rng}", "name": f"Mock {rng}"},
            "priceUsd": str(0.0001 * (rng + 1)),
            "marketCap": mcap,
            "fdv": mcap,
            "liquidity": {"usd": 50_000 + (rng * 5000)},
            "volume": {
                "m5": v5,
                "h1": v5 * 8,
                "h24": v5 * 50,
            },
            "txns": {
                "m5": {"buys": 20 + rng, "sells": 10 + rng // 2}
            },
            "pairCreatedAt": now_ms - (rng * 60_000),
            "url": f"https://dexscreener.com/solana/{address}",
        }

    async def close(self):
        await self._client.aclose()
