"""
pump.fun frontend API client for pre-migration sniping.

Unofficial endpoints from the pump.fun web frontend.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx

from src.utils.logger import get_logger

log = get_logger("pumpfun")

BASE_URL = "https://frontend-api.pump.fun"


class PumpFun:
    def __init__(self, mock_mode: bool = False):
        self.mock_mode = mock_mode
        self._client = httpx.AsyncClient(timeout=15.0)

    async def get_latest_coins(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get latest pump.fun coin launches."""
        if self.mock_mode:
            return self._mock_latest(limit)

        try:
            r = await self._client.get(f"{BASE_URL}/coins/latest", params={"limit": limit})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"pump.fun get_latest_coins failed: {e}")
            return []

    async def get_coins_by_criteria(self, min_mcap: float = 20_000,
                                     max_mcap: float = 80_000,
                                     min_age_min: int = 40) -> List[Dict[str, Any]]:
        """Get coins in the pre-migration zone."""
        all_coins = await self.get_latest_coins(limit=200)
        out = []
        for c in all_coins:
            mcap = c.get("usd_market_cap", 0)
            age_ms = c.get("created_timestamp", 0)
            age_min = (time.time() * 1000 - age_ms) / 60000 if age_ms else 0
            if min_mcap <= mcap <= max_mcap and age_min >= min_age_min:
                out.append({
                    "address": c.get("mint"),
                    "symbol": c.get("symbol", "???"),
                    "name": c.get("name", ""),
                    "mcap_usd": mcap,
                    "age_minutes": age_min,
                    "reply_count": c.get("reply_count", 0),
                    "king_of_the_hill": c.get("king_of_the_hill", False),
                    "raw": c,
                })
        return out

    def _mock_latest(self, limit: int) -> List[Dict[str, Any]]:
        now = time.time() * 1000
        return [
            {
                "mint": f"MOCKPF{i}So1111111111111111111111111111111111",
                "symbol": f"PF{i}",
                "name": f"PumpFun Mock {i}",
                "usd_market_cap": 25_000 + (i * 3000),
                "created_timestamp": int(now - (45 + i) * 60_000),
                "reply_count": 50 + i * 10,
                "king_of_the_hill": i == 5,
            }
            for i in range(limit)
        ]

    async def close(self):
        await self._client.aclose()
