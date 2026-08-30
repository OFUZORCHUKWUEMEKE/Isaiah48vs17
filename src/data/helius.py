"""
Helius API client for wallet transaction tracking.

Docs: https://docs.helius.dev/
Free tier: 50k credits/day.

Provides:
  - get_wallet_transactions(): recent transactions for a wallet
  - find_token_buys(): filter txs for token swap/buy events
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from src.utils.logger import get_logger

log = get_logger("helius")

BASE_URL = "https://api.helius.xyz"


class Helius:
    def __init__(self, api_key: str = "", mock_mode: bool = False):
        self.api_key = api_key
        self.mock_mode = mock_mode or not api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        if self.mock_mode:
            log.warning("Helius running in MOCK mode (no API key)")

    async def get_wallet_transactions(self, wallet: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent transactions for a wallet."""
        if self.mock_mode or "MOCK" in wallet or "REPLACE" in wallet:
            return self._mock_transactions(wallet, limit)

        try:
            url = f"{BASE_URL}/v0/addresses/{wallet}/transactions?api-key={self.api_key}&limit={limit}"
            r = await self._client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Helius get_wallet_transactions({wallet[:8]}) failed: {e}")
            return []

    async def find_recent_token_buys(self, wallet: str, lookback_minutes: int = 10) -> List[Dict[str, Any]]:
        """Find tokens the wallet has bought in the last N minutes."""
        txs = await self.get_wallet_transactions(wallet, limit=30)
        cutoff = time.time() - (lookback_minutes * 60)
        buys = []
        for tx in txs:
            ts = tx.get("timestamp", 0)
            if ts < cutoff:
                continue
            # Heuristic: SWAP type with positive token change
            if tx.get("type") in ("SWAP", "BUY") or "swap" in str(tx.get("description", "")).lower():
                token_changes = tx.get("tokenTransfers", []) or []
                for change in token_changes:
                    if change.get("toUserAccount") == wallet:
                        buys.append({
                            "wallet": wallet,
                            "token_mint": change.get("mint"),
                            "amount": change.get("tokenAmount", 0),
                            "timestamp": ts,
                            "tx_signature": tx.get("signature"),
                            "source": tx.get("source"),
                        })
        return buys

    def _mock_transactions(self, wallet: str, limit: int) -> List[Dict[str, Any]]:
        """Mock transactions for testing without API key."""
        # Simulate occasional buys by tracked wallets
        rng = hash(wallet + str(int(time.time() // 60))) % 10
        txs = []
        # 30% chance of having bought a token in the last 10 min
        if rng < 3:
            mock_mints = [
                "So11111111111111111111111111111111111111112",  # SOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTD",  # USDC
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB",  # BONK
            ]
            txs.append({
                "signature": f"mock_{wallet[:6]}_{int(time.time())}",
                "timestamp": int(time.time()) - 60,
                "type": "SWAP",
                "source": "JUPITER",
                "description": f"Swapped SOL for {mock_mints[rng % 3][:8]}",
                "tokenTransfers": [{
                    "mint": mock_mints[rng % 3],
                    "toUserAccount": wallet,
                    "tokenAmount": 1_000_000 + rng * 100_000,
                }],
            })
        return txs

    async def close(self):
        await self._client.aclose()
