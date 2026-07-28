"""
Approval queue: every candidate that passes risk_manager gates lands here
as PENDING, gets alerted via Telegram, and waits for a tap. Expired
requests are auto-rejected since option prices move.
"""

import sqlite3
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("approval")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    strategy TEXT,
    candidate_json TEXT,
    status TEXT DEFAULT 'PENDING',   -- PENDING, APPROVED, REJECTED, EXPIRED, EXECUTED, FAILED
    created_at TEXT,
    expires_at TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    approval_id TEXT,
    symbol TEXT,
    strategy TEXT,
    order_json TEXT,
    schwab_order_id TEXT,
    status TEXT,
    created_at TEXT
);
"""


def _connect(path_or_url: str):
    """Connect to SQLite file, or to a Turso/libsql DATABASE_URL if provided.

    Turso's libsql-experimental exposes a `connect()` call compatible with
    sqlite3 for the subset we use (execute, executescript, commit, row
    access). Setting DATABASE_URL in Render's Environment lets you persist
    trades/learning/guardrails history across deploys and restarts for free.
    """
    url = os.environ.get("DATABASE_URL")
    if url and (url.startswith("libsql://") or url.startswith("http://") or url.startswith("https://")):
        try:
            from libsql_experimental import connect as libsql_connect  # type: ignore
            token = os.environ.get("DATABASE_AUTH_TOKEN")
            kwargs = {"sync_url": url}
            if token:
                kwargs["auth_token"] = token
            path_for_local_file = path_or_url or os.environ.get("BOT_DATA_DIR", ".") + "/bot.db"
            return libsql_connect(path_for_local_file, **kwargs)
        except ImportError:
            log.warning(
                "DATABASE_URL is set (%s) but `pip install libsql-experimental` is missing. "
                "Falling back to local SQLite. Your data will NOT survive Render deploys/restarts.",
                url[:32] + "…",
            )
        except Exception as e:
            log.warning("Turso/libsql connect failed, falling back to local SQLite: %s", e)
    return sqlite3.connect(path_or_url, check_same_thread=False)


class Db:
    def __init__(self, path="./bot.db"):
        self.path = path
        self.conn = _connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def create_approval(self, candidate: dict, timeout_minutes: int) -> str:
        approval_id = str(uuid.uuid4())
        now = datetime.utcnow()
        expires = now + timedelta(minutes=timeout_minutes)
        self.conn.execute(
            "INSERT INTO approvals (id, symbol, strategy, candidate_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, candidate["symbol"], candidate["strategy"],
             json.dumps(candidate), now.isoformat(), expires.isoformat()),
        )
        self.conn.commit()
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, symbol, strategy, candidate_json, status, expires_at FROM approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "symbol": row[1], "strategy": row[2],
            "candidate": json.loads(row[3]), "status": row[4], "expires_at": row[5],
        }

    def set_status(self, approval_id: str, status: str):
        self.conn.execute(
            "UPDATE approvals SET status=?, decided_at=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), approval_id),
        )
        self.conn.commit()

    def expire_stale(self):
        now = datetime.utcnow().isoformat()
        cur = self.conn.execute(
            "UPDATE approvals SET status='EXPIRED' WHERE status='PENDING' AND expires_at < ?",
            (now,),
        )
        self.conn.commit()
        return cur.rowcount

    def count_trades_today(self, day_iso: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE created_at LIKE ?",
            (f"{day_iso}%",),
        ).fetchone()
        return row[0] if row else 0

    def record_trade(self, approval_id, symbol, strategy, order_json, schwab_order_id, status):
        trade_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO trades (id, approval_id, symbol, strategy, order_json, schwab_order_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, approval_id, symbol, strategy, json.dumps(order_json), schwab_order_id,
             status, datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return trade_id

    def open_position_risk_by_sector(self, sector_map: dict) -> dict:
        rows = self.conn.execute(
            "SELECT a.candidate_json FROM trades t JOIN approvals a ON t.approval_id = a.id "
            "WHERE t.status='EXECUTED' AND (t.outcome='OPEN' OR t.outcome IS NULL)"
        ).fetchall()
        by_sector = {}
        for (candidate_json,) in rows:
            c = json.loads(candidate_json)
            sector = sector_map.get(c.get("symbol", "").upper(), "Unknown")
            risk = (c.get("max_loss_per_contract") or 0) * (c.get("suggested_qty") or 1)
            by_sector[sector] = by_sector.get(sector, 0.0) + risk
        return by_sector