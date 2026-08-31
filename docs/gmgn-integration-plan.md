# Integrating GMGN OpenAPI into the Memecoin Runner Agent

> **Status: proposal — not implemented.** Design document only; no code in this
> repository has been changed. Line references point at the code as of this
> branch. Scope is read-only market/wallet data: no trade execution, no swap or
> cooking endpoints, no wallet private keys.

## Context

The agent currently polls DexScreener, enriches with Birdeye, applies a rulebook, and alerts on Telegram. Auditing the data path before designing the integration turned up that **most of the rulebook's decision inputs are fabricated**, and the alerts print those fabrications to Telegram as if they were measured:

| Field | Where | Reality |
|---|---|---|
| `pro_traders` | `loop.py:290` | Hardcoded `50`. Threshold is 40, so this check **always passes** and always adds score. |
| `fib_retracement` | `loop.py:291` | Hardcoded `0`. `0` isn't in `fib_entry_levels`, and the `elif fib_level > 0` branch is false — the fib check is a **permanent no-op**. |
| `volume_spike_ratio` | `dexscreener.py:161-162` | `vol1m = m5/5`, then `spike = m5/vol1m` — algebraically **exactly 5.0**, always. Threshold is 10.0, so `volume_spike` **can never fire**. |
| `volume_15m_usd` | `dexscreener.py:207` | Literally `m5 * 3`. Makes `check_volume_decay` return `decaying=True` for every token. |
| `top10_pct`, `insider_pct` | `birdeye.py:107-118` | Every failure path (no key, HTTP 429, any exception) silently returns `hash(address) % 100`-derived numbers. The guard at `birdeye.py:92` treats mock output as real. |
| `bundle_pct` | `birdeye.py:77-82` | Hash-derived in mock mode, hardcoded `0.0` in real mode. Never once real. |
| `win_rate`, `avg_roi` | `wallet_scorer.py:157-166` | Hardcoded to `0` by design — the docstring at `:258` says ROI can't be derived from on-chain data alone. Wallet scores have **no profitability input**. |

Net effect: of three strategies, `pre_migration_sniping` is disabled *and* structurally unreachable (pump.fun tokens carry no `volume_1h_usd`, so `passes_momentum_filter` at `loop.py:293` drops them all), `volume_spike` is mathematically incapable of passing, and `pullback` is the only live path — running on two constants and two hash-derived numbers.

There is also a throughput problem: DexScreener returns 50 tokens, each needing a Birdeye `enrich_token` throttled at 1.3s ≈ **65 seconds of enrichment inside a 30-second scan interval**.

GMGN's OpenAPI supplies real values for every one of these fields, and `/v1/market/rank` returns 100 pre-filtered candidates *already carrying* holder, bundler, rug and smart-money data in a single weight-1 request. So this is not a feature bolt-on — **it is the data-integrity fix that makes the existing rulebook mean something**, and it collapses the enrichment fan-out as a side effect.

Scope is read-only. No `swap`, no `cooking`, no trade execution, no wallet keys. The rule engine stays the decision authority; GMGN is a data source.

---

## Design

### 1. `src/data/gmgn.py` — new data source

Follows the existing data-source pattern exactly (module `BASE_URL` + `log`, eager `httpx.AsyncClient(timeout=15.0)`, `mock_mode = mock_mode or not api_key`, early `if self.mock_mode: return self._mock_*()` in every method, bare `try/except` → log → safe fallback, `async def close()`).

```python
class GMGNClient:
    def __init__(self, api_key="", base_url="", chain="sol",
                 mock_mode=False, transport="auto"): ...
```

**Transport abstraction.** GMGN's public docs only describe the `gmgn-cli` binary; the REST base URL is not published and could not be verified (docs.gmgn.ai is unreachable from this environment). A single private `_request(route, params, weight)` handles both:

- `transport="auto"` tries HTTP against `{base_url}{route}`.
- On a transport-level failure (DNS, 404 on a known-good route, auth rejection) it logs once, flips `self._transport = "cli"` for the rest of the process, and dispatches via `asyncio.create_subprocess_exec("gmgn-cli", ..., "--raw")`, parsing single-line JSON from stdout.
- Data-level failures (429, 5xx, malformed body) do **not** flip transport — they retry or fall back to mock.

Both paths return the same parsed dict, so nothing above `_request` knows which was used.

**Rate limiter.** GMGN uses a leaky bucket, `rate=20`, `capacity=20`, per-route weights, and returns a `reset_at` unix timestamp on 429. Implement `_LeakyBucket.acquire(weight)` as an `asyncio` primitive rather than copying Birdeye's fixed `_throttle()` sleep — the per-route weights (rank=1, kline=2, wallet_stats=3, top_holders=5) make a flat delay either wasteful or non-compliant. On 429, honor `reset_at` rather than backing off blindly.

**Methods** (all `async`, all with `_mock_*` counterparts):

| Method | Route | Weight |
|---|---|---|
| `get_rank(**filters) -> List[Dict]` | `/v1/market/rank` | 1 |
| `get_token_info(address)` | `/v1/token/info` | 1 |
| `get_token_security(address)` | `/v1/token/security` | 1 |
| `get_top_holders(address)` | `/v1/market/token_top_holders` | 5 |
| `get_kline(address, resolution, frm, to)` | `/v1/market/token_kline` | 2 |
| `get_wallet_stats(wallet)` | `/v1/user/wallet_stats` | 3 |
| `get_smartmoney_feed(**f)` / `get_kol_feed(**f)` | `/v1/user/smartmoney`, `/v1/user/kol` | 1 |
| `enrich_token(token) -> Dict` | — | — |

`enrich_token` deliberately mirrors `Birdeye.enrich_token`'s signature (`birdeye.py:84`) so it is a drop-in at the `loop.py:288` call site.

### 2. Field mapping

GMGN returns **rates as 0–1 ratios**; the engine reads **0–100 percents**. Every rate needs `× 100`.

| GMGN field | Token key (engine reads) | Conversion |
|---|---|---|
| `top_10_holder_rate` | `top10_pct` | × 100 |
| `suspected_insider_hold_rate` | `insider_pct` | × 100 |
| `bundler_trader_amount_rate` / `bundler_rate` | `bundle_pct` | × 100 — **note the key is `bundle_pct`, not `bundling_pct`** (`engine.py:119`) |
| `smart_degen_count` + `renowned_count` | `pro_traders` | sum — replaces the hardcoded `50` |
| `holder_count` | `holder_count` | as-is |
| `market_cap` / `liquidity` / `price` | `mcap_usd` / `liquidity_usd` / `price_usd` | as-is |
| `swaps` / `buys` / `sells` | `txns_1h_*`, `buy_sell_ratio_m5` | derive per interval |
| `dev_team_hold_rate` | `dev_hold_pct` | × 100 — new |
| `rug_ratio`, `is_wash_trading`, `creator_token_status` | same names | new gates (see §6) |

### 3. `src/rules/indicators.py` — new module, real K-line math

`/v1/market/token_kline` at `1m` resolution gives real per-candle `volume` (USD). Two pure functions, no I/O, trivially unit-testable:

- **`volume_metrics(candles_1m) -> dict`** — `volume_1m_usd` = last candle; `volume_5m_usd` = sum of last 5; `volume_15m_usd` = sum of last 15; and a genuine `volume_spike_ratio` = last-1m volume ÷ mean of the prior N candles (config `kline_lookback_candles`, default 60). This is the first time the value reflects an actual baseline instead of the constant 5.0.
- **`fib_retracement(candles_5m, lookback) -> float`** — swing high/low over the lookback window, then `(high - price) / (high - low)`, clamped to 0–1.

**Two engine fixes are required for these to work:**

1. `engine.py:206` matches fib with exact float equality (`if fib_level in cfg["fib_entry_levels"]`). Real retracements never land exactly on `0.618`. Replace with a tolerance check against a new `fib_tolerance` config (default `0.02`).
2. `check_volume_decay` (`engine.py:320-323`) computes `ratio_5m_to_1m = vol5m / (vol1m * 5)`. With real data this is **inverted**: a hot final minute makes `vol1m` large, the ratio small, and the token gets flagged as *decaying* — the opposite of the intent. It must become `vol1m / (vol5m / 5)` (recent minute vs. the 5-minute average). This bug is currently invisible because `vol1m` is synthetic; turning on real data would surface it as a wave of false "decaying" rejections.

### 4. Restructured `_scan_cycle`

`gmgn.get_rank(...)` becomes the **primary discovery path** — one weight-1 request returning up to 100 candidates that already carry holder, bundler, rug and smart-money fields. Server-side filters (`min_liquidity`, `max_rug_ratio`, `min_created`, `min_smart_degen_count`, platform) do work the agent currently does client-side, after paying for it.

DexScreener is retained as a secondary source. Merge on `address`: GMGN wins for holder/security/smart-money fields; DexScreener fills `price_change_m5_pct` and the `txns`-derived ratios where GMGN's interval shape doesn't cover them.

**This is what fixes the 65s-in-a-30s-window problem** — the per-token Birdeye fan-out disappears entirely, because discovery and enrichment arrive in the same response.

**K-line budget.** Klines are the one remaining per-token call (weight 2). Do **not** fetch them for all 100 candidates. Fetch only for the top `kline_budget_per_cycle` (default 8) tokens that survive `passes_momentum_filter`, cached per address with a short TTL. 8 × weight 2 = 16 units against a 20/sec bucket — comfortably sub-second.

### 5. Wallet signals — feed alongside Helius

GMGN **cannot** answer "which wallets bought token X"; it exposes wallet-scoped queries and platform-wide feeds. So the mint→wallet map is built by *inverting* a feed: poll `/v1/user/smartmoney` and `/v1/user/kol` (weight 1 each) on the existing `wallet_poll_interval`, filter to `side == "buy"`, `amount_usd >= min_wallet_buy_usd`, `timestamp >= cutoff`, then populate the existing `_recent_wallet_buys` / `_recent_buys_by_tier` structures.

**Tier mapping matters a great deal here.** In `apply_wallet_signal` (`engine.py:345-353`), tiers 1 and 2 *force Tier A*. Mapping GMGN's platform-wide smart-money feed to tier 2 would flood Tier A alerts and drown the curated list. So: **GMGN-sourced wallets map to tier 3 by default** (+10 score, no forced promotion), configurable via `feed_wallet_tier`. A wallet that appears in the feed *and* is on the curated 203 keeps its `WalletScorer` tier. The curated list stays the high-conviction signal; GMGN widens the net without diluting it.

**Prerequisite fix.** `loop.py:309` iterates `_recent_wallet_buys` to "pick the BEST wallet", but that dict is `mint → first buyer only` (`loop.py:436`) — there is never more than one candidate, so the tier selection is a no-op. Change it to `mint → list of (wallet, source)` before adding a second signal source, or the feed's contribution is silently discarded.

### 6. Wallet scoring — real profitability at last

`/v1/user/wallet_stats` returns `pnl_stat.winrate`, `roi`, the `pnl_gt_5x_num` / `pnl_2x_5x_num` distribution buckets, and `common.created_token_count` — precisely the data `wallet_scorer.py:258-259` says is underivable on-chain.

In `score_wallet` (`wallet_scorer.py:87`), when a GMGN client is available, fetch stats and populate the currently-zeroed `win_rate` and `avg_roi`. Then add a sixth component to `_composite_score` and **rescale** — the five existing weights already sum to 100 (25+25+20+15+15), so profitability can't just be appended:

| Component | Now | Proposed |
|---|---|---|
| Activity | 25 | 20 |
| Recency | 25 | 20 |
| Specialization | 20 | 15 |
| Hold discipline | 15 | 10 |
| Token diversity | 15 | 10 |
| **Profitability (new)** | — | **25** |

Also use `created_token_count > 0` to tag likely dev wallets.

**Budget:** 203 wallets × weight 3 = 609 units ÷ 20/sec ≈ **31 seconds** for a full pass. Fine as a periodic background pass, unacceptable per cycle. Note `is_cache_fresh()` (`wallet_scorer.py:76`) never persists `_last_full_run`, so it returns `False` after every restart — on Railway that means a full rescore on every redeploy. Persist it as part of this work.

### 7. Config and credentials

New `config.json` block:

```json
"gmgn": {
  "enabled": true, "chain": "sol", "base_url": "", "transport": "auto",
  "rank_interval": "5m", "rank_limit": 100, "rank_order_by": "volume",
  "max_rug_ratio": 0.30, "min_liquidity_usd": 20000, "min_created": "30m",
  "kline_resolution": "1m", "kline_budget_per_cycle": 8,
  "kline_lookback_candles": 60, "fib_lookback_candles": 288,
  "fib_tolerance": 0.02, "feed_wallet_tier": 3, "use_for_wallet_scoring": true
}
```

`.env.example`: `GMGN_API_KEY`, `GMGN_BASE_URL`, optional `GMGN_PRIVATE_KEY` (a request-*signing* key, explicitly **not** a wallet key — worth a comment saying so). `src/utils/config.py` adds these to the `_env` dict (`config.py:33`) and a `"gmgn"` entry to `has_real_credentials` (`config.py:44`).

**Threshold retuning is mandatory, not optional.** `min_pro_traders: 40` was calibrated against a hardcoded `50`. Real `smart_degen_count + renowned_count` is typically single-digit to low-tens, so leaving it at 40 would reject essentially everything the moment real data arrives — the bot would go silent and look "broken". Drop it to ~3–5 and re-tune from observed distributions. Same caution for `min_volume_spike_ratio: 10.0` once the ratio is real.

### 8. Telegram commands

Following the closure pattern in `register_commands` (`commands.py:24`, registered at `:293-304`, `/help` hand-maintained at `:30-46`):

- `/gmgn <address>` — token deep-dive: holders, insiders, bundlers, rug ratio, security flags, smart-money count
- `/trending` — top 10 from `/v1/market/rank`
- `/smart` — recent smart-money buys from the feed

**Caveat:** command handlers run on the polling thread via a throwaway `asyncio.new_event_loop()` (`telegram.py:239-242`), so any handler touching the agent's async clients crosses event loops — the pre-existing bug from the earlier review. These new commands must either read from a cache the main loop refreshes, or that dispatch must be fixed first (stage 0). Also note `commands.py:247` references an undefined `log` (`NameError`).

---

## Staged rollout

0. **Prerequisites** — fix the cross-event-loop command dispatch; change `_recent_wallet_buys` to `mint → list`; persist `_last_full_run`. Without these, later stages silently misbehave.
1. **`gmgn.py` skeleton** — transport, leaky bucket, mock mode. Validate the base URL and auth against the public read-only demo key (`gmgn_solbscbaseethmonadtron`) before anything depends on it. **This is the gate: if HTTP doesn't work, the CLI fallback is load-bearing and needs the binary in the Railway image.**
2. **Enrichment** — field mapping + `enrich_token`, wired behind `gmgn.enabled`, Birdeye retained as fallback. Verify with `python bot.py once`.
3. **Discovery** — `get_rank` as primary, merge with DexScreener, retune `min_pro_traders`.
4. **Indicators** — `indicators.py` + kline wiring + the two engine fixes (fib tolerance, decay inversion). `volume_spike` becomes capable of firing for the first time.
5. **Feed inversion** — smart-money/KOL into the wallet-buy structures at tier 3.
6. **Wallet scoring** — profitability component + rescaled weights.
7. **Telegram commands.**

---

## Verification

The repo has **zero tests**, which is why the fabricated-data problems survived this long. Add `tests/` (pytest) alongside the integration:

- **Fixtures** — record real GMGN responses to `tests/fixtures/*.json` using the demo key. Enables offline testing of the whole mapping layer.
- **Unit: field mapping** — assert `0.42 → 42.0` for every rate, and that `bundle_pct` (not `bundling_pct`) is populated.
- **Unit: indicators** — synthetic candle arrays with known answers. A flat series must give `volume_spike_ratio ≈ 1.0` (not 5.0); a 10× final candle must give ≈ 10.0. Fib: a known high/low/price triple must land on 0.618 within tolerance.
- **Regression: the dead rules** — a golden test asserting `evaluate_volume_spike` **can** now return `passed=True` on a constructed spike token, and that the fib branch fires. These lock in the actual fix.
- **End-to-end** — `python bot.py test-rules` (mock path), then `python bot.py once` against the live demo key, confirming real `top10_pct` values appear in verdicts instead of hash-derived ones.

A useful one-off check before trusting the data: run the old and new enrichment side by side on the same 20 tokens and diff `top10_pct` — it quantifies how wrong the Birdeye path was.

---

## Risks and explicit non-goals

- **Do not** integrate `/gmgn-swap` or `/gmgn-cooking`, set `GMGN_ALLOW_AUTOMATED_TRADES`, or place any wallet private key in the repo or environment. The project is paper-only by design; execution is a different risk class. (Their swap path also needs a whitelisted static egress IP, which Railway doesn't provide by default.)
- **Do not** let GMGN become the decision-maker. The rulebook is the project's actual thesis; GMGN feeds it inputs. Alerting on "GMGN says smart money bought" would reduce this to a thin wrapper around someone else's product.
- **Do not** delete Birdeye or Helius. Keep them as fallbacks so a GMGN outage degrades the agent instead of killing it.
- **Unconfirmed base URL** is the biggest unknown — stage 1 exists specifically to de-risk it before anything is built on top.
- **Silence-after-integration is the likely failure mode.** Thresholds tuned against constants will reject real data. Expect to re-tune; watch alert volume closely for the first day.
- Pricing and quota for GMGN are undocumented publicly — confirm before depending on it in a 30-second loop.
- Separately worth fixing (from the earlier review): `_seen_addresses` (`loop.py:66`) grows unbounded and permanently suppresses any token alerted once. With GMGN returning 100 candidates a cycle instead of 50, this will bite sooner.
