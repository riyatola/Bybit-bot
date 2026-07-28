"""
Tiny stdlib HTTP health server for Render / Fly / App Service health probes.

Exposes:
  GET /              -> 200 text "ok" (+ uptime, scan cycle stats if available)
  GET /healthz       -> 200 JSON {"status":"ok", ...}
  GET /readyz        -> 200 if bybit connectivity checked at least once, else 503
  Any other path     -> 404

Render's free Web Service tier considers a container alive as long as it
returns HTTP 2xx on port $PORT within 60 seconds of boot. Ping this
endpoint from UptimeRobot every 5 minutes to keep Render's free instance
from being idled (they idle after 15 minutes of zero inbound traffic).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("health")

_STATE = {
    "started_at": time.time(),
    "last_scan_at": None,
    "last_exit_check_at": None,
    "bybit_auth_ok": False,
    "telegram_ok": False,
}


def mark_event(name: str) -> None:
    """Call this from scanner.py after a successful cycle so /healthz shows timings."""
    if name == "scan":
        _STATE["last_scan_at"] = time.time()
    elif name == "exit_check":
        _STATE["last_exit_check_at"] = time.time()
    elif name == "bybit_auth":
        _STATE["bybit_auth_ok"] = True
    elif name == "telegram":
        _STATE["telegram_ok"] = True


def _payload() -> dict:
    now = time.time()
    return {
        "status": "ok",
        "service": "crypto-options-bot",
        "uptime_seconds": round(now - _STATE["started_at"], 1),
        "last_scan_seconds_ago": (
            round(now - _STATE["last_scan_at"], 1) if _STATE["last_scan_at"] else None
        ),
        "last_exit_check_seconds_ago": (
            round(now - _STATE["last_exit_check_at"], 1) if _STATE["last_exit_check_at"] else None
        ),
        "bybit_auth_ok": _STATE["bybit_auth_ok"],
        "telegram_ok": _STATE["telegram_ok"],
        "hostname": socket.gethostname(),
        "data_dir": os.environ.get("BOT_DATA_DIR", "."),
        "mode": os.environ.get("BOT_MODE", "unknown"),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "CryptoOptionsBot/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter than default
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = (self.path or "/").split("?", 1)[0]
        if path == "/":
            body = f"ok uptime={_payload()['uptime_seconds']}s\n".encode()
            self._send(200, "text/plain; charset=utf-8", body)
        elif path == "/healthz":
            body = json.dumps(_payload()).encode()
            self._send(200, "application/json", body)
        elif path == "/readyz":
            payload = _payload()
            ready = payload["bybit_auth_ok"]
            body = json.dumps({**payload, "ready": ready}).encode()
            self._send(200 if ready else 503, "application/json", body)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


def _resolve_port() -> int:
    raw = os.environ.get("PORT") or os.environ.get("HTTP_PORT") or "8080"
    try:
        return max(1, min(65535, int(raw)))
    except (TypeError, ValueError):
        return 8080


async def start_health_server_async() -> ThreadingHTTPServer:
    """Start the HTTP server in a thread so it never blocks the asyncio loop.

    We keep the server I/O on a background thread rather than trying to
    share an async HTTP server with python-telegram-bot's event loop.
    This avoids any deadlock or scheduling pressure on the scheduler/
    strategy workloads, and stdlib ThreadingHTTPServer is more than
    enough for the ~120/hour health pings Render + UptimeRobot send.
    """
    port = _resolve_port()
    host = "0.0.0.0"
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _serve, server)
    return server


def _serve(server: ThreadingHTTPServer) -> None:
    try:
        log.info("Health server listening on %s:%d", *server.server_address)
        server.serve_forever(poll_interval=0.5)
    except Exception as e:
        log.error("Health server exited: %s", e)


def get_port() -> int:
    return _resolve_port()
