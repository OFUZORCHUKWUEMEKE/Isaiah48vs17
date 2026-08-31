"""Centralized config and env loader."""
import json
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.json"
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)


def load_config() -> Dict[str, Any]:
    """Load config.json with environment variable overrides."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.json not found at {CONFIG_PATH}")

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # Environment overrides
    mode = os.getenv("AGENT_MODE")
    if mode:
        cfg["mode"] = mode

    # Inject API keys (strip whitespace/newlines from env vars)
    def _env(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    cfg["_env"] = {
        "telegram_bot_token": _env("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": _env("TELEGRAM_CHAT_ID"),
        "helius_api_key": _env("HELIUS_API_KEY"),
        "birdeye_api_key": _env("BIRDEYE_API_KEY"),
        "log_level": _env("LOG_LEVEL", "INFO"),
    }

    return cfg


def has_real_credentials(cfg: Dict[str, Any]) -> Dict[str, bool]:
    """Check which APIs have real keys vs need mock fallback."""
    env = cfg.get("_env", {})
    return {
        "telegram": bool(env.get("telegram_bot_token") and env.get("telegram_chat_id")),
        "helius": bool(env.get("helius_api_key")),
        "birdeye": bool(env.get("birdeye_api_key")),
    }
