#!/usr/bin/env python3
"""
Memecoin Runner Agent - Main Entry Point

Usage:
  python bot.py                # Run the agent (default mode from config)
  python bot.py --once         # Run one scan cycle and exit
  python bot.py --stats        # Show paper ledger stats
  python bot.py --test-rules   # Test the rule engine on mock data
  python bot.py --mock         # Force mock mode for all data sources
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_config
from src.utils.logger import get_logger
from src.rules.engine import RuleEngine
from src.data.dexscreener import DexScreener
from src.data.birdeye import Birdeye

log = get_logger("bot")


def cmd_run(args):
    from src.agent.loop import MemecoinAgent
    cfg = load_config()
    # Safely read --mock (only present when subparser is used)
    if getattr(args, "mock", False):
        cfg["_env"]["helius_api_key"] = ""
        cfg["_env"]["birdeye_api_key"] = ""
        cfg["_env"]["gmgn_api_key"] = ""
        cfg["_env"]["telegram_bot_token"] = ""
        cfg["_env"]["telegram_chat_id"] = ""
        log.info("Forced MOCK mode for all APIs")
    agent = MemecoinAgent(cfg)
    asyncio.run(agent.run())


def cmd_once(args):
    """Run a single scan cycle and print verdicts."""
    from src.agent.loop import MemecoinAgent
    cfg = load_config()
    if getattr(args, "mock", False):
        cfg["_env"]["helius_api_key"] = ""
        cfg["_env"]["birdeye_api_key"] = ""
        cfg["_env"]["gmgn_api_key"] = ""
    agent = MemecoinAgent(cfg)

    async def one():
        # Same discovery + evaluation path _scan_cycle uses (GMGN primary
        # discovery, enrichment, momentum filter, real K-line indicators
        # for survivors, rules, wallet-tier boost) - see
        # MemecoinAgent._discover_runners and _evaluate_candidates. Calling
        # agent.dex or agent._enrich directly here, or hand-rolling the
        # evaluation steps, would bypass whichever of those the real cycle
        # does - which is exactly the class of bug that made this need
        # fixing twice already, once per stage (stage 2: PR #5's enrichment
        # call; stage 3: PR #6's discovery call and a missing enrichment
        # guard).
        #
        # This does mean cmd_once now applies the momentum filter and
        # wallet-tier boost too, which the old hand-rolled version did not:
        # a token that would never reach evaluate() during a real scan
        # cycle no longer gets printed here either - matching what
        # "run a single scan cycle" actually implies.
        runners = await agent._discover_runners()
        log.info(f"Found {len(runners)} runners")
        verdicts = await agent._evaluate_candidates(runners[:10])
        for verdict in verdicts:
            print("\n" + verdict.to_alert())
        await agent._shutdown()
    asyncio.run(one())


def cmd_stats(args):
    """Show paper ledger stats."""
    from src.agent.ledger import PaperLedger
    ledger = PaperLedger()
    stats = ledger.stats()
    print(json.dumps(stats, indent=2))


def cmd_test_rules(args):
    """Test rule engine against mock data."""
    cfg = load_config()
    engine = RuleEngine(cfg["rules"])
    dex = DexScreener(mock_mode=True)
    birdeye = Birdeye(mock_mode=True)

    async def test():
        pairs = await dex.search_pairs("solana")
        print(f"Testing against {len(pairs)} mock pairs\n")
        for raw in pairs:
            token = dex._normalize(raw)
            token = await birdeye.enrich_token(token)
            token["pro_traders"] = 50
            token["fib_retracement"] = 0.786 if "PULLBACK" in raw.get("baseToken", {}).get("symbol", "") else 0
            verdict = engine.evaluate(token)
            print(verdict.to_alert())
            print("-" * 50)
        await dex.close()
        await birdeye.close()
    asyncio.run(test())


def main():
    parser = argparse.ArgumentParser(description="Memecoin Runner Agent")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the agent continuously")
    p_run.add_argument("--mock", action="store_true", help="Force mock mode")

    p_once = sub.add_parser("once", help="Run one scan cycle")
    p_once.add_argument("--mock", action="store_true")

    sub.add_parser("stats", help="Show paper ledger stats")
    sub.add_parser("test-rules", help="Test rules on mock data")

    args = parser.parse_args()
    handlers = {
        "run": cmd_run,
        "once": cmd_once,
        "stats": cmd_stats,
        "test-rules": cmd_test_rules,
    }
    if args.cmd is None:
        # No subcommand: default to "run" in production mode.
        # Build a clean args namespace so handlers can safely use
        # getattr(args, "mock", False).
        args = argparse.Namespace(cmd="run", mock=False)
    handler = handlers[args.cmd]
    handler(args)


if __name__ == "__main__":
    main()
