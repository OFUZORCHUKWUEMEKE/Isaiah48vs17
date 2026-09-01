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

# Free tier is ~10 req/sec. Every caller shares one client, so pacing lives
# here rather than in the callers - two callers each pacing themselves to
# "just under the limit" still add up to double the limit.
MIN_REQUEST_INTERVAL = 0.15
# How long to stand down after a 429 that carries no Retry-After header.
DEFAULT_COOLDOWN = 5.0


class HeliusError(Exception):
    """A Helius request failed.

    Raised (rather than returning []) so callers can tell "this wallet has no
    transactions" apart from "we never found out". Caching a failure as an
    empty result silently blanks the wallet signal until the cache expires.
    """


class Helius:
    def __init__(self, api_key: str = "", mock_mode: bool = False,
                 min_interval: float = MIN_REQUEST_INTERVAL):
        self.api_key = api_key
        self.mock_mode = mock_mode or not api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        # Shared pacing state. The lock is only held across the wait, not the
        # request, so requests still overlap in flight - it exists to stop
        # concurrent callers (scan loop + scorer) from bursting past the limit.
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cooldown_until = 0.0
        self._min_interval = min_interval
        if self.mock_mode:
            log.warning("Helius running in MOCK mode (no API key)")

    def _redact(self, text: str) -> str:
        """Strip the API key out of anything headed for the logs.

        httpx puts the full request URL in its exception messages, and the key
        travels as a query parameter, so logging a raw exception would write
        the credential to the log on every failure.
        """
        if self.api_key:
            text = text.replace(self.api_key, "***REDACTED***")
        return text

    async def _throttle(self):
        """Space out requests across every caller sharing this client."""
        async with self._lock:
            now = time.monotonic()
            wait = 0.0
            if now < self._cooldown_until:
                wait = self._cooldown_until - now
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                wait = max(wait, self._min_interval - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def _note_rate_limit(self, response: Optional[httpx.Response]) -> float:
        """Record a 429 and return how long to back off for."""
        retry_after = DEFAULT_COOLDOWN
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = max(0.0, float(raw))
                except (TypeError, ValueError):
                    pass
        self._cooldown_until = time.monotonic() + retry_after
        return retry_after

    async def get_wallet_transactions(self, wallet: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent transactions for a wallet.

        Returns [] when the wallet genuinely has no matching transactions.
        Raises HeliusError when the request failed, so callers can avoid
        caching a failure as if it were an empty wallet.
        """
        if self.mock_mode or "MOCK" in wallet or "REPLACE" in wallet:
            return self._mock_transactions(wallet, limit)

        url = f"{BASE_URL}/v0/addresses/{wallet}/transactions"
        params = {"api-key": self.api_key, "limit": limit}

        # One retry: a 429 is worth waiting out, anything else usually isn't.
        for attempt in (1, 2):
            await self._throttle()
            try:
                r = await self._client.get(url, params=params)
                if r.status_code == 429:
                    backoff = self._note_rate_limit(r)
                    if attempt == 1:
                        log.warning(
                            f"Helius rate-limited on {wallet[:8]}, "
                            f"backing off {backoff:.1f}s"
                        )
                        continue
                    raise HeliusError(f"rate limited (429) after retry, wallet {wallet[:8]}")
                r.raise_for_status()
                return r.json()
            except HeliusError:
                raise
            except Exception as e:
                # Never log the exception directly - it carries the full URL,
                # and the URL carries the API key.
                detail = self._redact(str(e))
                log.error(
                    f"Helius get_wallet_transactions({wallet[:8]}) failed: "
                    f"{type(e).__name__}: {detail}"
                )
                raise HeliusError(detail) from None

        raise HeliusError(f"exhausted retries for wallet {wallet[:8]}")

    async def find_recent_token_buys(self, wallet: str, lookback_minutes: int = 10) -> List[Dict[str, Any]]:
        """Find tokens the wallet has bought in the last N minutes.

        Returns [] on failure - callers of this helper treat "no buys" and
        "couldn't check" the same way.
        """
        try:
            txs = await self.get_wallet_transactions(wallet, limit=30)
        except HeliusError:
            return []
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
