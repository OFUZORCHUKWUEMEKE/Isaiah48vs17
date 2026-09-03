"""
GMGN OpenAPI client for token discovery, holder/security data, K-lines,
and wallet/smart-money feeds.

Docs: https://gmgn.ai/ai (skill docs at github.com/GMGNAI/gmgn-skills)

Read-only. Deliberately does not implement /gmgn-swap or /gmgn-cooking
(trade execution) - see docs/gmgn-integration-plan.md for why.

Transport note: GMGN's public docs describe the `gmgn-cli` binary; a
public REST base URL is not documented and was not reachable from this
development environment to verify directly. This client tries HTTP first
and falls back to shelling out to `gmgn-cli --raw` on a transport-level
failure. The CLI route mapping below is reconstructed from the skill
docs, NOT from a live run of the binary - verify each mapping against
`gmgn-cli --help` before depending on the CLI fallback in production.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import httpx

from src.utils.logger import get_logger

log = get_logger("gmgn")

DEFAULT_BASE_URL = "https://api.gmgn.ai"

# Leaky-bucket rate limit per GMGN's docs: rate=20, capacity=20, cost=weight.
BUCKET_RATE = 20.0
BUCKET_CAPACITY = 20.0
# Cooldown to use on a 429 that carries no reset_at.
DEFAULT_COOLDOWN = 3.0
# Route weights, from the skill docs.
WEIGHTS = {
    "rank": 1,
    "token_info": 1,
    "token_security": 1,
    "top_holders": 5,
    "kline": 2,
    "wallet_stats": 3,
    "smartmoney_feed": 1,
    "kol_feed": 1,
}


class GMGNError(Exception):
    """A GMGN request reached the server but failed (429/5xx/malformed body).

    Distinct from a transport failure - the server responded, it just
    didn't give us data. Callers should not treat this as "no signal";
    see the HeliusError precedent in src/data/helius.py for why silently
    returning an empty result on failure is the wrong default here.
    """


class _TransportUnavailable(Exception):
    """HTTP transport can't reach the API at all (DNS/connect/404/auth).

    Internal signal to fall back to the CLI transport. Distinct from
    GMGNError: the server never answered, so there's nothing to retry
    against over HTTP - only routing away helps.
    """


class _LeakyBucket:
    """Token-bucket limiter shared across every caller of one client.

    Same rationale as Helius's shared throttle: two independent callers
    each pacing to "under the limit" can still add up to over it. One
    bucket, held behind a lock, makes that impossible regardless of how
    many call sites use this client concurrently.
    """

    def __init__(self, rate: float = BUCKET_RATE, capacity: float = BUCKET_CAPACITY):
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    async def acquire(self, weight: float = 1.0):
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self._cooldown_until:
                    await asyncio.sleep(self._cooldown_until - now)
                    now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                wait = (weight - self._tokens) / self.rate
                await asyncio.sleep(wait)

    def note_cooldown(self, seconds: float):
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.0, seconds))


class GMGNClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        chain: str = "sol",
        mock_mode: bool = False,
        transport: str = "auto",
    ):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.chain = chain
        self.mock_mode = mock_mode or not api_key
        # "auto" tries HTTP, falls back to CLI on a transport failure, then
        # stays on CLI for the rest of the process (no flapping back to a
        # base URL that's already been shown not to work).
        self._transport = "mock" if self.mock_mode else ("http" if transport == "auto" else transport)
        self._client = httpx.AsyncClient(timeout=15.0)
        self._bucket = _LeakyBucket()
        if self.mock_mode:
            log.warning("GMGN running in MOCK mode (no API key)")

    def _redact(self, text: str) -> str:
        if self.api_key:
            text = text.replace(self.api_key, "***REDACTED***")
        return text

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _request(self, route: str, params: Dict[str, Any], route_key: str) -> Any:
        """Fetch one route, mock/HTTP/CLI as appropriate. Raises GMGNError
        on a reachable-but-failed request; never returns None on failure.
        """
        weight = WEIGHTS.get(route_key, 1)
        await self._bucket.acquire(weight)

        if self._transport == "cli":
            return await self._request_cli(route_key, params)

        try:
            return await self._request_http(route, params)
        except _TransportUnavailable as e:
            log.warning(
                f"GMGN HTTP transport unavailable ({self._redact(str(e))}); "
                "falling back to gmgn-cli for the rest of this session"
            )
            self._transport = "cli"
            return await self._request_cli(route_key, params)

    async def _request_http(self, route: str, params: Dict[str, Any]) -> Any:
        url = f"{self.base_url}{route}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = await self._client.get(url, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol) as e:
            raise _TransportUnavailable(f"{type(e).__name__}: {e}") from None
        except Exception as e:
            raise GMGNError(self._redact(f"{type(e).__name__}: {e}")) from None

        if r.status_code == 404:
            raise _TransportUnavailable(f"404 at {route} - base_url is probably wrong")
        if r.status_code in (401, 403):
            raise _TransportUnavailable(f"HTTP {r.status_code} - auth rejected over HTTP")
        if r.status_code == 429:
            wait = self._note_rate_limit_http(r)
            self._bucket.note_cooldown(wait)
            raise GMGNError(f"rate limited (429), cooldown {wait:.1f}s")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise GMGNError(self._redact(str(e))) from None
        try:
            return r.json()
        except Exception as e:
            raise GMGNError(f"malformed response body: {type(e).__name__}") from None

    def _note_rate_limit_http(self, response: httpx.Response) -> float:
        try:
            body = response.json()
            reset_at = body.get("reset_at")
            if reset_at:
                return max(0.0, float(reset_at) - time.time())
        except Exception:
            pass
        return DEFAULT_COOLDOWN

    # Best-effort reconstruction from the skill docs (github.com/GMGNAI/
    # gmgn-skills) - NOT verified against a live gmgn-cli. Each entry is
    # (subcommand words, positional/flag mapping). Confirm with
    # `gmgn-cli <noun> --help` before relying on this path.
    _CLI_MAP = {
        "token_info": ["token", "info"],
        "token_security": ["token", "security"],
        "top_holders": ["token", "holders"],
        "wallet_stats": ["portfolio", "stats"],
        "smartmoney_feed": ["track", "smartmoney"],
        "kol_feed": ["track", "kol"],
        # Unconfirmed subcommand names - the skill docs describe these by
        # function ("Trending/Rankings Command", "K-line (OHLCV) Command")
        # but never quote the literal gmgn-cli invocation.
        "rank": ["market", "rank"],
        "kline": ["market", "kline"],
    }

    async def _request_cli(self, route_key: str, params: Dict[str, Any]) -> Any:
        subcommand = self._CLI_MAP.get(route_key)
        if not subcommand:
            raise GMGNError(f"no CLI mapping for route '{route_key}'")

        args = ["gmgn-cli", *subcommand, "--chain", self.chain]
        for k, v in params.items():
            if v is None:
                continue
            args.extend([f"--{k.replace('_', '-')}", str(v)])
        args.append("--raw")

        env = {"GMGN_API_KEY": self.api_key} if self.api_key else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env or None,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except FileNotFoundError:
            raise GMGNError("gmgn-cli not found on PATH - CLI fallback needs the binary installed") from None
        except asyncio.TimeoutError:
            raise GMGNError(f"gmgn-cli timed out: {' '.join(subcommand)}") from None

        if proc.returncode != 0:
            raise GMGNError(self._redact(f"gmgn-cli exit {proc.returncode}: {stderr.decode(errors='replace')[:200]}"))
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError as e:
            raise GMGNError(f"gmgn-cli produced non-JSON output: {e}") from None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_rank(self, **filters: Any) -> List[Dict[str, Any]]:
        """Trending/discovery candidates. filters: interval, limit, order_by,
        min_liquidity, max_rug_ratio, min_created, platform, etc.
        """
        if self.mock_mode:
            return self._mock_rank(**filters)
        try:
            params = {"chain": self.chain, **filters}
            data = await self._request("/v1/market/rank", params, "rank")
            return data.get("data", data) if isinstance(data, dict) else data
        except GMGNError as e:
            log.error(f"GMGN get_rank failed: {e}")
            return []

    def _normalize_rank_item(self, item: Dict[str, Any], interval: str) -> Dict[str, Any]:
        """Convert one /v1/market/rank item into the shared token-dict shape
        DexScreener._normalize() also produces, so a GMGN-sourced candidate
        can flow through momentum filtering and evaluate() unmodified.

        CAVEAT: the exact field name for the token address on a rank item
        is not confirmed against a live response (same unverified-API
        caveat as the rest of this module - see the module docstring and
        docs/gmgn-integration-plan.md). Falls back across a few plausible
        names rather than assuming one.

        `interval` is whatever rank_interval the caller queried GMGN with.
        swaps/buys/sells describe that single window, not "5m" and "1h"
        simultaneously - only the txns_* keys matching that window are
        populated here. The discovery step in loop.py merges in DexScreener
        afterward to fill the other window for tokens both sources saw.
        """
        addr = item.get("address") or item.get("token_address") or item.get("base_address") or ""
        buys = item.get("buys", 0) or 0
        sells = item.get("sells", 0) or 0
        mcap = item.get("market_cap", 0) or 0
        fdv = item.get("fdv", mcap) or mcap
        smart = (item.get("smart_degen_count") or 0) + (item.get("renowned_count") or 0)

        token: Dict[str, Any] = {
            "address": addr,
            "symbol": item.get("symbol", "?"),
            "name": item.get("name", item.get("symbol", "?")),
            "chain": self.chain,
            "dex": "gmgn",
            "price_usd": item.get("price", 0) or 0,
            "mcap_usd": mcap,
            "fdv_usd": fdv,
            "liquidity_usd": item.get("liquidity", 0) or 0,
            "fdv_mcap_ratio": (fdv / mcap) if mcap else 0,
            "holder_count": item.get("holder_count", 0) or 0,
            # Holder/security/smart-money - GMGN's actual value-add over
            # DexScreener. Rates already converted 0-1 -> 0-100 here, same
            # conversion as enrich_token(), so this dict needs no further
            # enrichment pass (loop.py's `if "top10_pct" not in token`
            # guard skips it for these tokens - see docs/gmgn-integration-plan.md #4).
            "top10_pct": (item.get("top_10_holder_rate") or 0) * 100,
            "insider_pct": (item.get("suspected_insider_hold_rate") or 0) * 100,
            "bundle_pct": (item.get("bundler_rate", item.get("bundler_trader_amount_rate", 0)) or 0) * 100,
            "dev_hold_pct": (item.get("dev_team_hold_rate") or 0) * 100,
            "pro_traders": smart,
            "rug_ratio": item.get("rug_ratio", 0) or 0,
            "is_wash_trading": bool(item.get("is_wash_trading", False)),
            "creator_token_status": item.get("creator_token_status", ""),
            "gmgn_source": True,
        }

        if interval == "5m":
            token["volume_5m_usd"] = item.get("volume", 0) or 0
            token["buy_sell_ratio_m5"] = (buys / sells) if sells else float(buys)
        elif interval == "1h":
            token["volume_1h_usd"] = item.get("volume", 0) or 0
            token["txns_1h_buys"] = buys
            token["txns_1h_sells"] = sells
        else:
            # Other intervals (1m/6h/24h/...): stash under their own key
            # rather than mislabeling as a 5m or 1h figure the engine reads.
            token[f"volume_{interval}_usd"] = item.get("volume", 0) or 0

        return token

    async def get_discovery_candidates(self, **filters: Any) -> List[Dict[str, Any]]:
        """get_rank() + normalization into the shared token-dict shape.

        This is what loop.py's discovery step calls. get_rank() itself
        stays available unnormalized for raw API access (e.g. a future
        /trending command that wants to show GMGN's own field names).
        """
        interval = str(filters.get("interval", "5m"))
        items = await self.get_rank(**filters)
        out = []
        for item in items:
            token = self._normalize_rank_item(item, interval)
            if token.get("address"):
                out.append(token)
            else:
                log.debug("GMGN rank item had no recognizable address field, skipped")
        return out

    async def get_token_info(self, address: str) -> Dict[str, Any]:
        if self.mock_mode or "MOCK" in address:
            return self._mock_token_info(address)
        try:
            data = await self._request(
                "/v1/token/info", {"chain": self.chain, "address": address}, "token_info"
            )
            return data.get("data", data) if isinstance(data, dict) else {}
        except GMGNError as e:
            log.error(f"GMGN get_token_info({address[:8]}) failed: {e}")
            return {}

    async def get_token_security(self, address: str) -> Dict[str, Any]:
        if self.mock_mode or "MOCK" in address:
            return self._mock_token_security(address)
        try:
            data = await self._request(
                "/v1/token/security", {"chain": self.chain, "address": address}, "token_security"
            )
            return data.get("data", data) if isinstance(data, dict) else {}
        except GMGNError as e:
            log.error(f"GMGN get_token_security({address[:8]}) failed: {e}")
            return {}

    async def get_top_holders(self, address: str, **filters: Any) -> Dict[str, Any]:
        if self.mock_mode or "MOCK" in address:
            return self._mock_top_holders(address)
        try:
            params = {"chain": self.chain, "address": address, **filters}
            data = await self._request("/v1/market/token_top_holders", params, "top_holders")
            return data.get("data", data) if isinstance(data, dict) else {}
        except GMGNError as e:
            log.error(f"GMGN get_top_holders({address[:8]}) failed: {e}")
            return {}

    async def get_kline(
        self, address: str, resolution: str = "1m",
        frm: Optional[int] = None, to: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if self.mock_mode or "MOCK" in address:
            return self._mock_kline(address, resolution)
        try:
            params = {"chain": self.chain, "address": address, "resolution": resolution}
            if frm is not None:
                params["from"] = frm
            if to is not None:
                params["to"] = to
            data = await self._request("/v1/market/token_kline", params, "kline")
            return data.get("data", data) if isinstance(data, dict) else data
        except GMGNError as e:
            log.error(f"GMGN get_kline({address[:8]}) failed: {e}")
            return []

    async def get_wallet_stats(self, wallet: str) -> Dict[str, Any]:
        if self.mock_mode or "MOCK" in wallet or "REPLACE" in wallet:
            return self._mock_wallet_stats(wallet)
        try:
            data = await self._request(
                "/v1/user/wallet_stats", {"chain": self.chain, "wallet": wallet}, "wallet_stats"
            )
            return data.get("data", data) if isinstance(data, dict) else {}
        except GMGNError as e:
            log.error(f"GMGN get_wallet_stats({wallet[:8]}) failed: {e}")
            return {}

    async def get_smartmoney_feed(self, **filters: Any) -> List[Dict[str, Any]]:
        if self.mock_mode:
            return self._mock_feed("smartmoney")
        try:
            params = {"chain": self.chain, **filters}
            data = await self._request("/v1/user/smartmoney", params, "smartmoney_feed")
            return data.get("data", data) if isinstance(data, dict) else data
        except GMGNError as e:
            log.error(f"GMGN get_smartmoney_feed failed: {e}")
            return []

    async def get_kol_feed(self, **filters: Any) -> List[Dict[str, Any]]:
        if self.mock_mode:
            return self._mock_feed("kol")
        try:
            params = {"chain": self.chain, **filters}
            data = await self._request("/v1/user/kol", params, "kol_feed")
            return data.get("data", data) if isinstance(data, dict) else data
        except GMGNError as e:
            log.error(f"GMGN get_kol_feed failed: {e}")
            return []

    async def enrich_token(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Add holder/insider/bundler/smart-money fields to a token dict.

        Mirrors Birdeye.enrich_token's signature (birdeye.py:84) so it is a
        drop-in at the loop.py enrichment call site. Always succeeds - on
        failure the token is returned with whatever it already had, via
        setdefault, same fallback discipline as Birdeye.
        """
        addr = token.get("address", "")
        if not addr:
            return token

        holders = await self.get_top_holders(addr)
        if holders:
            # GMGN rates are 0-1; the rule engine reads 0-100 percents.
            if "top_10_holder_rate" in holders:
                token["top10_pct"] = (holders.get("top_10_holder_rate") or 0) * 100
            if "suspected_insider_hold_rate" in holders:
                token["insider_pct"] = (holders.get("suspected_insider_hold_rate") or 0) * 100
            if "bundler_trader_amount_rate" in holders:
                token["bundle_pct"] = (holders.get("bundler_trader_amount_rate") or 0) * 100
            if "dev_team_hold_rate" in holders:
                token["dev_hold_pct"] = (holders.get("dev_team_hold_rate") or 0) * 100
            if "holder_count" in holders:
                token["holder_count"] = holders.get("holder_count", 0)
            smart = holders.get("wallet_tags_stat", {}) or {}
            if smart:
                token["pro_traders"] = (
                    smart.get("smart_wallets", 0) + smart.get("renowned_wallets", 0)
                )
        else:
            mock = self._mock_top_holders(addr)
            token.setdefault("top10_pct", mock["top_10_holder_rate"] * 100)
            token.setdefault("insider_pct", mock["suspected_insider_hold_rate"] * 100)
            token.setdefault("bundle_pct", mock["bundler_trader_amount_rate"] * 100)
            token.setdefault("pro_traders", 3)
        return token

    async def close(self):
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Mock data - deterministic per address/wallet, same pattern as the
    # other data sources (hash-derived, no network).
    # ------------------------------------------------------------------
    def _mock_rank(self, limit: int = 20, **_: Any) -> List[Dict[str, Any]]:
        out = []
        for i in range(min(limit, 20)):
            addr = f"MOCK{i}GMGNRankxxxxxxxxxxxxxxxxxxxxxxxxxxx"[:44]
            rng = hash(addr) % 100
            out.append({
                "address": addr,
                "symbol": f"MOCK{i}",
                "price": 0.0001 + rng * 1e-6,
                "market_cap": 50_000 + rng * 1000,
                "liquidity": 20_000 + rng * 500,
                "volume": 10_000 + rng * 300,
                "swaps": 20 + rng,
                "buys": 12 + rng % 10,
                "sells": 8 + rng % 8,
                "holder_count": 100 + rng,
                "rug_ratio": (rng % 30) / 100,
                "is_wash_trading": False,
                "top_10_holder_rate": (20 + rng % 30) / 100,
                "bundler_rate": (rng % 20) / 100,
                "smart_degen_count": rng % 8,
                "renowned_count": rng % 3,
                "creator_token_status": "creator_close",
            })
        return out

    def _mock_token_info(self, address: str) -> Dict[str, Any]:
        rng = hash(address) % 100
        return {"address": address, "symbol": f"MOCK{rng}", "price": 0.0001, "market_cap": 50_000 + rng * 1000}

    def _mock_token_security(self, address: str) -> Dict[str, Any]:
        rng = hash(address) % 100
        return {
            "rug_ratio": (rng % 30) / 100,
            "owner_renounced": rng % 2 == 0,
            "renounced_mint": rng % 2 == 0,
            "renounced_freeze_account": rng % 2 == 0,
        }

    def _mock_top_holders(self, address: str) -> Dict[str, Any]:
        rng = hash(address) % 100
        return {
            "top_10_holder_rate": (20 + rng % 30) / 100,
            "suspected_insider_hold_rate": (5 + rng % 20) / 100,
            "bundler_trader_amount_rate": (rng % 25) / 100,
            "dev_team_hold_rate": (rng % 15) / 100,
            "holder_count": 100 + rng * 10,
            "wallet_tags_stat": {"smart_wallets": rng % 6, "renowned_wallets": rng % 2},
        }

    def _mock_kline(self, address: str, resolution: str) -> List[Dict[str, Any]]:
        rng = hash(address) % 100
        now_ms = int(time.time() * 1000)
        step_ms = {"30s": 30_000, "1m": 60_000, "5m": 300_000, "15m": 900_000,
                   "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(resolution, 60_000)
        candles = []
        price = 0.0001
        for i in range(60):
            t = now_ms - (60 - i) * step_ms
            vol = 100 + ((rng + i) % 20) * 10
            candles.append({
                "time": t, "open": price, "close": price, "high": price, "low": price,
                "volume": vol, "amount": vol / max(price, 1e-9),
            })
        return candles

    def _mock_wallet_stats(self, wallet: str) -> Dict[str, Any]:
        rng = hash(wallet) % 100
        return {
            "realized_profit": (rng - 50) * 100,
            "roi": (rng - 50) / 100,
            "pnl_stat": {
                "token_num": 5 + rng % 20,
                "winrate": (30 + rng % 50) / 100,
                "pnl_gt_5x_num": rng % 3,
                "pnl_2x_5x_num": rng % 5,
            },
            "common": {"created_token_count": rng % 2},
        }

    def _mock_feed(self, kind: str) -> List[Dict[str, Any]]:
        rng = int(time.time() // 60) % 10
        if rng >= 3:
            return []
        return [{
            "transaction_hash": f"mock_{kind}_{int(time.time())}",
            "maker": f"MOCK{kind}WalletXxxxxxxxxxxxxxxxxxxxxxxxxxxx"[:44],
            "side": "buy",
            "base_address": f"MOCK{kind}TokenXxxxxxxxxxxxxxxxxxxxxxxxxxxx"[:44],
            "amount_usd": 200 + rng * 50,
            "timestamp": int(time.time()) - 30,
        }]
