"""
Minimal HTTP health check server for Railway / Render / any host
that needs an HTTP endpoint to consider the service "alive".

Exposes:
  GET /health  → 200 OK with basic status
  GET /status  → JSON with full agent status (tiers, last scan, etc.)
  GET /        → 200 OK (fallback for HEAD requests)

Railway's healthcheck (configured in railway.json) hits /health.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
import threading

from src.utils.logger import get_logger
from src.version import BUILD_MARKER, BUILD_NOTE

log = get_logger("health")

# Global reference to the agent so the HTTP handler can introspect
_agent_ref: Optional[Any] = None
_server_thread: Optional[threading.Thread] = None
_started_at: float = time.time()


def set_agent(agent):
    """Called from the agent's run() to expose status to the health endpoint."""
    global _agent_ref
    _agent_ref = agent


def start_health_server(port: int = 8080):
    """Start the HTTP health server in a daemon thread.
    Returns immediately; the server runs in the background.
    """
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        log.info("Health server already running")
        return

    handler = _make_handler()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    except OSError as e:
        log.warning(f"Could not start health server on port {port}: {e}")
        return

    _server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    _server_thread.start()
    log.info(f"Health server listening on :{port} (GET /health, /status)")


def _make_handler():
    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Silence default access logging
            pass

        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                # build is here (not just /status) so that confirming which
                # code is live needs no agent state and no log access.
                self._json(200, {
                    "status": "ok",
                    "uptime_s": int(time.time() - _started_at),
                    "service": "memecoin-runner-agent",
                    "build": BUILD_MARKER,
                    "build_note": BUILD_NOTE,
                })
            elif self.path == "/status":
                self._json(200, _get_status())
            else:
                self._json(404, {"error": "not found"})

        def do_HEAD(self):
            if self.path in ("/", "/health", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def _json(self, code: int, body: Dict[str, Any]):
            payload = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return HealthHandler


def _get_status() -> Dict[str, Any]:
    """Build a status dict from the running agent (if available)."""
    if _agent_ref is None:
        return {"status": "starting", "agent": None}

    try:
        stats = _agent_ref.ledger.stats() if _agent_ref.ledger else {}
        breakdown = _agent_ref.scorer.get_tier_breakdown() if _agent_ref.scorer else {}
        return {
            "status": "running",
            "build": BUILD_MARKER,
            "uptime_s": int(time.time() - _started_at),
            "mode": _agent_ref.cfg.get("mode"),
            "gmgn": {
                "enabled": getattr(_agent_ref, "gmgn_enabled", None),
                "transport": getattr(getattr(_agent_ref, "gmgn", None), "_transport", None),
                "base_url": getattr(getattr(_agent_ref, "gmgn", None), "base_url", None),
            },
            "tracked_wallets": len(_agent_ref.tracked_wallets),
            "tier_breakdown": breakdown,
            "alerts_sent_today": _agent_ref.alerter._today_count,
            "daily_cap": _agent_ref.daily_cap,
            "paused": _agent_ref.alerter.is_paused(),
            "paper_pnl": stats,
            "last_scan": getattr(_agent_ref, "_last_scan_at", None),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
