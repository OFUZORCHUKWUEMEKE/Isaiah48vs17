"""
Telegram alerter with mock fallback.

If real credentials are present, sends real alerts and polls for commands
(/stats, /pause, /resume, /positions, /help) from the configured chat.
"""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from src.utils.logger import get_logger
from src.rules.engine import Verdict, Tier

log = get_logger("telegram")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERTS_LOG = PROJECT_ROOT / "data" / "alerts.log"

# How long the polling thread waits for a command before replying "still
# running" and moving on. A full /wallets rescore over 200+ wallets is the
# slowest command and needs generous headroom.
COMMAND_TIMEOUT_SECONDS = 240


class TelegramAlerter:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        # Two clients: one for sending (short timeout), one for long-polling
        self._client = httpx.AsyncClient(timeout=10.0)
        self._poll_client = httpx.AsyncClient(timeout=60.0)
        # Sync client for the polling thread (long-poll from a separate thread)
        self._sync_send_client = None
        self._today_count = 0
        self._today_date = datetime.utcnow().date()
        self._paused = False
        self._command_handlers: Dict[str, Callable] = {}
        self._polling_thread = None
        self._stop_polling = __import__('threading').Event()
        self._offset = 0
        # The agent's event loop, captured when polling starts. Command
        # handlers are submitted back onto it rather than run on a throwaway
        # loop, because they touch httpx clients that are bound to it.
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        # Last reply context for commands that need richer data
        self._ctx: Dict[str, Any] = {}

        if not self.enabled:
            log.warning("Telegram running in MOCK mode (no credentials)")
        ALERTS_LOG.parent.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Command registration
    # ------------------------------------------------------------------
    def register_command(self, name: str, handler: Callable[[str], Awaitable[str]]):
        """Register a /command handler. Handler receives the args string, returns response text."""
        self._command_handlers[name] = handler
        log.debug(f"Registered Telegram command: /{name}")

    def set_context(self, **kwargs):
        """Set context that command handlers can access (e.g. agent state)."""
        self._ctx.update(kwargs)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def send_verdict(self, verdict: Verdict, daily_cap: int = 50) -> bool:
        """Send a verdict alert. Returns True if sent, False if rate-limited."""
        if self._paused:
            log.debug("Alerts paused - skipping verdict")
            return False

        # Daily cap check
        today = datetime.utcnow().date()
        if today != self._today_date:
            self._today_count = 0
            self._today_date = today
        if self._today_count >= daily_cap:
            log.warning(f"Daily alert cap ({daily_cap}) reached. Skipping.")
            return False

        # Only send Tier A and B (high-conviction)
        if verdict.tier == Tier.C:
            return False

        text = verdict.to_alert()
        ok = await self._send(text)
        if ok:
            self._today_count += 1
            self._log_to_file(verdict, text)
        return ok

    async def send_text(self, text: str) -> bool:
        """Send a freeform text message."""
        return await self._send(text)

    async def _send(self, text: str) -> bool:
        if not self.enabled:
            log.info(f"[MOCK TELEGRAM] Would send:\n{text}\n")
            return True
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            r = await self._client.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                log.error(f"Telegram send failed ({r.status_code}): {r.text[:200]}")
                return False
            return True
        except Exception as e:
            log.error(f"Telegram send failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Command polling (long-polling getUpdates)
    # ------------------------------------------------------------------
    def start_polling(self):
        """Start the background command-polling thread.
        Uses a thread (not asyncio task) so it doesn't compete with the
        agent's main event loop for the same httpx connection pool.
        """
        import threading
        if not self.enabled:
            log.warning("Telegram polling disabled (no credentials)")
            return
        if self._polling_thread and self._polling_thread.is_alive():
            log.warning("Polling already running")
            return
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            # Started outside a running loop (tests, CLI one-shots). Handlers
            # fall back to asyncio.run(), which is fine as long as nothing
            # else is using the shared clients concurrently.
            self._main_loop = None
            log.warning(
                "start_polling() called with no running event loop - "
                "commands will run on a temporary loop"
            )
        self._stop_polling.clear()
        self._polling_thread = threading.Thread(
            target=self._poll_thread_loop, daemon=True
        )
        self._polling_thread.start()
        log.info("Telegram command polling started (thread)")

    def stop_polling(self):
        """Stop the background polling thread."""
        self._stop_polling.set()
        if self._polling_thread:
            self._polling_thread.join(timeout=5)
            self._polling_thread = None
        log.info("Telegram command polling stopped")

    def _poll_thread_loop(self):
        """Thread-based long-poll for incoming messages.
        Uses synchronous httpx to avoid interfering with the asyncio event loop.

        Runs 20s long-polls, then immediately starts the next one. Per-request
        clients avoid stale-connection issues; a `connect=5, read=25` timeout
        ensures we never block longer than 25s even on a stuck server.
        """
        import httpx as sync_httpx
        if self._sync_send_client is None:
            self._sync_send_client = sync_httpx.Client(timeout=10.0)

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        poll_count = 0
        consecutive_errors = 0
        while not self._stop_polling.is_set():
            client = None
            try:
                timeouts = sync_httpx.Timeout(connect=5.0, read=25.0, write=5.0, pool=5.0)
                client = sync_httpx.Client(timeout=timeouts)
                r = client.get(
                    url,
                    params={"timeout": 20, "offset": self._offset, "allowed_updates": '["message"]'},
                )
                poll_count += 1
                if r.status_code == 409:
                    # Conflict: another bot instance is using getUpdates.
                    # Skip the current message batch and bump offset to catch up.
                    log.warning("getUpdates 409 conflict — another session is polling. Skipping ahead.")
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    next_offset = data.get("parameters", {}).get("retry_after", 0) or (self._offset + 1)
                    self._offset = max(self._offset + 1, next_offset)
                    self._stop_polling.wait(5)
                    continue
                if r.status_code != 200:
                    log.warning(f"getUpdates failed ({r.status_code})")
                    consecutive_errors += 1
                    self._stop_polling.wait(min(30, 2 ** consecutive_errors))
                    continue
                consecutive_errors = 0  # reset on success
                data = r.json()
                msgs = data.get("result", [])
                if msgs:
                    log.info(f"Polled #{poll_count}: {len(msgs)} new message(s)")
                for update in msgs:
                    self._offset = max(self._offset, update["update_id"] + 1)
                    self._handle_update_sync(update)
                # Heartbeat: log every 10 successful polls so we can see it's alive
                if poll_count % 10 == 0:
                    log.debug(f"Polling alive: {poll_count} polls, offset={self._offset}")
            except Exception as e:
                log.error(f"Polling error: {e}")
                consecutive_errors += 1
                self._stop_polling.wait(min(30, 2 ** consecutive_errors))
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    def _handle_update_sync(self, update: Dict[str, Any]):
        """Synchronous version of update handler for thread use.
        Uses sync httpx to avoid asyncio event loop issues.
        """
        import httpx as sync_httpx
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        log.info(f"📩 Received message: {text!r} from chat {chat_id!r} (configured: {self.chat_id!r})")

        if chat_id != str(self.chat_id):
            log.warning(f"Chat ID mismatch: got {chat_id!r}, expected {self.chat_id!r}. Ignoring.")
            return

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        log.info(f"⚡ Processing command: /{cmd} args={args!r}")

        handler = self._command_handlers.get(cmd)
        if not handler:
            log.info(f"Unknown command: /{cmd}")
            self._send_sync(f"❓ Unknown command: /{cmd}\nType /help for available commands.")
            return

        # Submit the handler onto the agent's own event loop.
        #
        # Running it on a fresh loop here would break every shared
        # httpx.AsyncClient: their connection pools hold asyncio primitives
        # bound to the loop that created them, which surfaces as
        # "<Event ...> is bound to a different event loop". It also let
        # command work run *concurrently* with the scan loop, so two
        # independent callers hammered the same rate-limited APIs.
        try:
            if self._main_loop is not None and not self._main_loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(handler(args), self._main_loop)
                try:
                    response = future.result(timeout=COMMAND_TIMEOUT_SECONDS)
                except FuturesTimeoutError:
                    # The coroutine keeps running on the agent loop; we just
                    # stop waiting so the poller isn't blocked indefinitely.
                    log.warning(f"Command /{cmd} still running after {COMMAND_TIMEOUT_SECONDS}s")
                    self._send_sync(
                        f"⏳ /{cmd} is taking longer than {COMMAND_TIMEOUT_SECONDS}s. "
                        "It's still running — check back shortly."
                    )
                    return
            else:
                response = asyncio.run(handler(args))
            log.info(f"📤 Sending response for /{cmd} ({len(response)} chars)")
            self._send_sync(response)
        except Exception as e:
            log.error(f"Command /{cmd} failed: {e}", exc_info=True)
            self._send_sync(f"⚠️ Error handling /{cmd}: {e}")

    def _send_sync(self, text: str) -> bool:
        """Synchronous send used by the polling thread."""
        import httpx as sync_httpx
        if self._sync_send_client is None:
            self._sync_send_client = sync_httpx.Client(timeout=10.0)
        if not self.enabled:
            log.info(f"[MOCK TELEGRAM] Would send:\n{text}\n")
            return True
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            r = self._sync_send_client.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                log.error(f"Telegram send failed ({r.status_code}): {r.text[:200]}")
                return False
            return True
        except Exception as e:
            log.error(f"Telegram send failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    def pause(self):
        self._paused = True
        log.info("Alerts paused")

    def resume(self):
        self._paused = False
        log.info("Alerts resumed")

    def is_paused(self) -> bool:
        return self._paused

    def _log_to_file(self, verdict: Verdict, text: str):
        with open(ALERTS_LOG, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"{datetime.utcnow().isoformat()}Z\n")
            f.write(json.dumps({
                "tier": verdict.tier.value,
                "symbol": verdict.symbol,
                "address": verdict.token_address,
                "score": verdict.score,
                "strategy": verdict.strategy,
            }, indent=2) + "\n")
            f.write(text + "\n")

    async def close(self):
        self.stop_polling()
        await self._client.aclose()
        await self._poll_client.aclose()
        if self._sync_send_client:
            self._sync_send_client.close()
