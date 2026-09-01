# Memecoin Runner Agent 🤖

A Solana on-chain AI agent that monitors memecoin runners, tracks smart wallets, and alerts you on Telegram when the rulebook fires.

Built on the **`memecoin-trading-robot-rules`** skill — every threshold here comes from a curated YouTube trading strategy.

---

## ✨ What It Does

Every 30 seconds, the agent:

1. **Polls DexScreener** for high-volume Solana pairs (BONK, WIF, POPCAT, new launches)
2. **Pulls pump.fun pre-migration candidates** (20k-80k MCAP zone)
3. **Enriches with holder data** (top-10 concentration, insider %, bundling)
4. **Applies the rulebook** (4 strategies, 30+ thresholds)
5. **Cross-references tracked wallets** (Helius) — upgrades to Tier A
6. **Sends Telegram alerts** on Tier A/B signals (max 50/day)
7. **Tracks paper positions** in `data/paper_ledger.json` (no real money)

You see the alert. You click buy. Agent does the research.

---

## 🎯 Strategies (from the rulebook)

| Strategy | Source | When It Fires |
|----------|--------|---------------|
| **Pre-migration sniping** | Rule 2.1 | pump.fun coin in 20-40k MCAP, age ≥ 40min, top-10 ≤ 40%, insiders ≤ 35%, bundling ≤ 25% |
| **Pullback trading** | Rule 2.2 | Established coin (≥ $3M MCAP) bouncing off 61.8% or 78.6% Fib with volume |
| **Volume spike** | Rule 2.4 | 10x+ 5m volume spike on < 500k SOL liquidity (potential 10x-50x run) |
| **Wallet signal** | Your requirement | Tracked wallet just bought → upgrade any passing verdict to Tier A |

**Skipped for v1**: Axiom Pro execution (paid dep), Twitter/Telegram signal mining (complex, low quality).

---

## 🚀 Quick Start

```bash
# 1. Install
cd ~/projects/memecoin-runner-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Test rules against mock data
python bot.py test-rules

# 3. Run one real scan cycle (uses real DexScreener, mock everything else)
python bot.py once

# 4. Start the agent (mock mode — logs to file, no real alerts)
python bot.py run --mock
```

---

## 🔑 Add Real API Keys (Recommended)

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
nano .env
```

| Key | Where | Free? |
|-----|-------|-------|
| `TELEGRAM_BOT_TOKEN` | Talk to @BotFather | ✅ |
| `TELEGRAM_CHAT_ID` | Talk to @userinfobot | ✅ |
| `HELIUS_API_KEY` | https://helius.dev | ✅ 50k credits/day |
| `BIRDEYE_API_KEY` | https://birdeye.so | ✅ |

Once added, the agent auto-detects and uses real data. No code changes needed.

---

## 📂 Project Structure

```
memecoin-runner-agent/
├── bot.py                      # Main entry point
├── config.json                 # All thresholds + tracked wallets
├── .env.example                # API key template
├── requirements.txt
├── data/
│   ├── alerts.log              # All sent alerts
│   └── paper_ledger.json       # Hypothetical positions
├── logs/
│   └── agent.log               # Rotating logs
└── src/
    ├── data/
    │   ├── dexscreener.py     # Volume, MCAP, pairs
    │   ├── helius.py          # Wallet transactions
    │   ├── birdeye.py         # Holder concentration
    │   └── pumpfun.py         # Pre-migration launches
    ├── rules/
    │   └── engine.py          # The rulebook (every threshold)
    ├── alerts/
    │   └── telegram.py        # Tiered alerts
    ├── agent/
    │   ├── loop.py            # Main 30s scan loop
    │   └── ledger.py          # Paper trading
    └── utils/
        ├── config.py          # Config + env loader
        └── logger.py          # Structured logging
```

---

## 🎛️ Configuration

All thresholds live in `config.json`. Tune them without touching code:

```jsonc
{
  "scanning": {
    "scan_interval_seconds": 30,
    "wallet_poll_interval_seconds": 60,
    "daily_alert_cap": 50
  },
  "tracked_wallets": [
    "YOUR_WALLET_ADDRESS_1",
    "YOUR_WALLET_ADDRESS_2"
  ],
  "rules": {
    "pre_migration_sniping": {
      "min_mcap_usd": 20000,
      "max_mcap_usd": 40000,
      "min_coin_age_minutes": 40,
      "max_top10_holder_pct": 40.0
      // ... all thresholds here
    }
  }
}
```

---

## 🚦 Alert Tiers

| Tier | Meaning | Sends to Telegram? |
|------|---------|---------------------|
| 🟢 **A** | Rule pass + tracked wallet buying | ✅ Yes |
| 🟡 **B** | Rule pass only | ✅ Yes |
| 🔴 **C** | Watch / soft signal | ❌ No (logged only) |

The agent will never spam you — daily cap is 50, per-cycle cap is 10, and Tier C never alerts.

---

## 📊 Commands

```bash
python bot.py run              # Start the agent (uses real keys if .env set)
python bot.py run --mock       # Force mock mode for all APIs
python bot.py once             # Run one scan cycle and exit
python bot.py stats            # Show paper ledger stats
python bot.py test-rules       # Test rules against mock data
```

---

## 🧪 Testing Without Real Money

The agent runs in **paper mode by default**. Every alert that would fire records a hypothetical position. Check `data/paper_ledger.json` for:

- What tokens you would have entered
- At what MCAP
- Whether they hit TP/SL
- Your overall PnL

Run for 1-2 weeks before going live. Tune thresholds based on the data.

---

## ⚠️ Disclaimer

Memecoin trading is **extremely high risk**. The rulebook comes from public YouTube strategies and is provided as-is. The agent is a **research tool**, not financial advice. Most memecoins go to zero. Never deploy more than you can lose.

---

## 🛠️ Next Steps

- [ ] Add your real tracked wallets to `config.json`
- [ ] Add real API keys to `.env`
- [ ] Run `python bot.py run` for 24h and check `data/paper_ledger.json`
- [ ] Tune thresholds based on what you see
- [ ] (Optional) Wire to a wallet signer for auto-execution

---

## 🚂 Deploy to Railway (24/7 hosting)

Railway keeps the bot running 24/7 with auto-restart on crash, a health check endpoint, and ~$5/month free credit (more than enough for this tiny workload).

### One-time setup

1. **Push code to GitHub**
   ```bash
   cd ~/projects/memecoin-runner-agent
   git init
   git add .
   git commit -m "Initial deploy"
   gh repo create memecoin-runner-agent --public --source=. --remote=origin --push
   ```
   (or create the repo on github.com and push manually)

2. **Create Railway account** at https://railway.app (sign up with GitHub)

3. **New Project → Deploy from GitHub repo** → select `memecoin-runner-agent`

4. **Set environment variables** in Railway dashboard → Variables:
   ```
   TELEGRAM_BOT_TOKEN = <your-bot-token-from-@BotFather>
   TELEGRAM_CHAT_ID   = <your-numeric-chat-id>
   HELIUS_API_KEY     = <your-helius-key>
   BIRDEYE_API_KEY    = <your-birdeye-key>
   AGENT_MODE         = paper
   LOG_LEVEL          = INFO
   ```
   ⚠️ **Do not put real values in code, in this README, or anywhere in git.**
   Enter them only in the Railway dashboard, where they're stored encrypted.
   Keep local values in `.env` (already gitignored) — see `.env.example`.
   If a credential is ever committed, rotate it immediately: revoking the
   Telegram token via `@BotFather` → `/revoke` is the only thing that
   actually invalidates it. Removing it from git does not.

5. **Deploy**: Railway auto-detects `Procfile` and `railway.json`, builds with Nixpacks, and starts the bot.

6. **Verify**: Check the deploy logs for "MEMECOIN RUNNER AGENT - STARTING". You should get a Telegram "Agent started" message within ~6 minutes (5 min scoring + 30s first scan).

7. **Health check**: Railway pings `GET /health` (configured in `railway.json`). Visit `https://your-app.up.railway.app/status` in a browser to see live agent stats.

### What happens on each deploy

- Wallet scores are recomputed on startup (~5-6 min warm-up)
- Telegram polling thread starts immediately
- Daily summary fires at 23:55 UTC
- SIGTERM is handled gracefully — bot sends a "stopped" message before exit
- On crash, Railway restarts automatically (`restartPolicyType: ON_FAILURE`)

### Local dev after deploy

`config.json` and `data/wallet_scores.json` are committed to git. To pull the latest on local:
```bash
git pull
source venv/bin/activate
python bot.py run --mock  # or with real .env
```

### Cost

Free tier: $5/month credit. This bot uses ~$1-2/month (just API calls + tiny compute). You'll get a free month of trial, then it'll just be ~$1/month.

---

## 📜 License

MIT — fork it, modify it, ship it.
