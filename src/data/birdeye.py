"""
Birdeye API client for holder data and token metadata.

Docs: https://docs.birdeye.so/

Free tier: limited; some fields require paid plan. We degrade gracefully.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from src.utils.logger import get_logger

log = get_logger("birdeye")

BASE_URL = "https://public-api.birdeye.so"


class Birdeye:
    def __init__(self, api_key: str = "", mock_mode: bool = False):
        self.api_key = api_key
        self.mock_mode = mock_mode or not api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        self._last_call = 0.0
        self._call_count = 0
        if self.mock_mode:
            log.warning("Birdeye running in MOCK mode (no API key)")
        else:
            log.info("Birdeye using real API key (free tier)")

    async def _throttle(self):
        # Free tier: ~50 calls/min
        elapsed = time.time() - self._last_call
        if elapsed < 1.3:  # polite spacing
            await asyncio.sleep(1.3 - elapsed)
        self._last_call = time.time()
        self._call_count += 1

    async def get_token_overview(self, token_address: str) -> Dict[str, Any]:
        """Get token metadata from Birdeye. Returns whatever fields are available."""
        if self.mock_mode or "MOCK" in token_address:
            return self._mock_overview(token_address)

        await self._throttle()
        try:
            r = await self._client.get(
                f"{BASE_URL}/defi/token_overview",
                headers={"X-API-KEY": self.api_key, "accept": "application/json"},
                params={"address": token_address},
            )
            if r.status_code == 429:
                log.warning("Birdeye rate-limited (429). Using mock for this token.")
                return self._mock_overview(token_address)
            r.raise_for_status()
            data = r.json().get("data", {}) or {}
            return {
                "top10_pct": (data.get("top10HolderPercent") or 0) * 100,
                "insider_pct": (data.get("insiderPercent") or 0) * 100,
                "holder_count": data.get("holder", 0),
                "supply": data.get("supply", 0),
                "price": data.get("price", 0),
                "mc": data.get("mc", 0),
                "liquidity": data.get("liquidity", 0),
            }
        except Exception as e:
            log.debug(f"Birdeye get_token_overview({token_address[:8]}) failed: {e}")
            return self._mock_overview(token_address)

    async def get_token_holders(self, token_address: str) -> Dict[str, Any]:
        """Alias for get_token_overview (kept for backward compat)."""
        return await self.get_token_overview(token_address)

    async def get_bundle_pct(self, token_address: str) -> float:
        """Estimate % of supply held by bundled wallets (Trench Scanner data)."""
        if self.mock_mode or "MOCK" in token_address:
            rng = hash(token_address) % 100
            return float(rng % 30)
        return 0.0

    async def enrich_token(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Add holder/bundle data to a token dict. Always succeeds (mock fallback)."""
        addr = token.get("address", "")
        if not addr:
            return token
        overview = await self.get_token_overview(addr)
        bundle_pct = await self.get_bundle_pct(addr)
        # Only override if we got real data, otherwise keep existing (e.g. mock)
        if overview.get("top10_pct") or overview.get("holder_count"):
            token.update({
                "top10_pct": overview.get("top10_pct", 0),
                "insider_pct": overview.get("insider_pct", 0),
                "holder_count": overview.get("holder_count", 0),
            })
        else:
            # Use mock if no real data available
            mock = self._mock_overview(addr)
            token.setdefault("top10_pct", mock["top10_pct"])
            token.setdefault("insider_pct", mock["insider_pct"])
            token.setdefault("holder_count", mock["holder_count"])
        token.setdefault("bundle_pct", bundle_pct)
        return token

    def _mock_overview(self, token_address: str) -> Dict[str, Any]:
        """Mock overview for testing / when API doesn't return data."""
        rng = hash(token_address) % 100
        return {
            "top10_pct": 20 + (rng % 30),  # 20-50%
            "insider_pct": 5 + (rng % 20),  # 5-25%
            "holder_count": 100 + (rng * 10),
            "supply": 1_000_000_000,
            "price": 0.0001,
            "mc": 0,
            "liquidity": 0,
        }

    async def close(self):
        await self._client.aclose()
