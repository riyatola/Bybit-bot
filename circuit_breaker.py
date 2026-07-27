"""
Circuit breaker + kill switch: prevents a bad day from getting worse.
- Tracks daily realized + unrealized P&L
- Trips if daily loss exceeds limit
- Manual kill switch
"""

import logging
import sqlite3
from datetime import datetime, date
import json

log = logging.getLogger("circuit_breaker")


class CircuitBreaker:
    def __init__(self, cfg: dict, db, client, account_hash: str, paper_mode: bool = False):
        self.cfg = cfg
        self.cb_cfg = cfg.get("circuit_breaker", {})
        self.enabled = self.cb_cfg.get("enabled", True)
        self.daily_loss_limit = self.cb_cfg.get("daily_loss_limit_dollars", 1000)
        self.manual_kill = self.cb_cfg.get("manual_kill", False)
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        self.paper_mode = paper_mode

        # Ensure circuit breaker table exists
        self._ensure_table()

    def _ensure_table(self):
        """Create circuit_breaker table if not exists."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                is_tripped BOOLEAN DEFAULT 0,
                tripped_at TEXT,
                reason TEXT,
                manual_kill BOOLEAN DEFAULT 0,
                daily_realized_pnl REAL DEFAULT 0,
                daily_unrealized_pnl REAL DEFAULT 0,
                total_daily_pnl REAL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()
        # Ensure today's entry exists
        today = date.today().isoformat()
        exists = self.conn.execute(
            "SELECT id FROM circuit_breaker WHERE date = ?", (today,)
        ).fetchone()
        if not exists:
            self.conn.execute(
                "INSERT INTO circuit_breaker (date, is_tripped, manual_kill) VALUES (?, 0, ?)",
                (today, self.manual_kill),
            )
            self.conn.commit()

    def calculate_daily_pnl(self):
        """Calculate total daily P&L (realized + unrealized)."""
        today = date.today().isoformat()

        # First, get realized P&L from trades closed today
        realized = 0.0
        trades = self.conn.execute("""
            SELECT realized_pnl FROM trades
            WHERE created_at >= ? AND outcome IN ('WIN', 'LOSS', 'BREAKEVEN', 'CLOSED_UNKNOWN_PNL')
            AND realized_pnl IS NOT NULL
        """, (today + "T00:00:00",)).fetchall()
        for (pnl,) in trades:
            realized += pnl

        # Unrealized P&L (TODO: we need to calculate this for open positions)
        # For now, let's just use 0, but we can refine later
        # Also, in paper mode, we can use the paper broker's account
        unrealized = 0.0
        if self.paper_mode:
            try:
                from paper_broker import PaperBrokerClient
                if hasattr(self.client, "get_account_state"):
                    account = self.client.get_account_state()
                    # Assuming paper broker tracks unrealized P&L
                    # For now, let's skip this, we'll circle back
                    pass
            except Exception:
                log.warning("Couldn't get paper broker unrealized P&L")

        total = realized + unrealized

        # Update today's entry
        self.conn.execute("""
            UPDATE circuit_breaker SET
                daily_realized_pnl = ?,
                daily_unrealized_pnl = ?,
                total_daily_pnl = ?
            WHERE date = ?
        """, (realized, unrealized, total, today))
        self.conn.commit()

        return total

    def is_tripped(self):
        """Check if circuit breaker is tripped (manual or automatic)."""
        if not self.enabled:
            return False

        today = date.today().isoformat()
        row = self.conn.execute("""
            SELECT is_tripped, manual_kill FROM circuit_breaker WHERE date = ?
        """, (today,)).fetchone()
        if row:
            is_tripped, manual_kill = row
            if manual_kill or self.manual_kill:
                return True
            return bool(is_tripped)
        return False

    def check_and_trip(self):
        """Check if daily loss exceeds limit, trip if needed."""
        if not self.enabled:
            return False

        today = date.today().isoformat()
        total_pnl = self.calculate_daily_pnl()
        # Check if loss (negative) exceeds limit (positive)
        if total_pnl <= -self.daily_loss_limit:
            # Trip the circuit breaker
            self.conn.execute("""
                UPDATE circuit_breaker SET
                    is_tripped = 1,
                    tripped_at = ?,
                    reason = ?
                WHERE date = ?
            """, (datetime.now().isoformat(), f"Daily loss limit hit: ${-total_pnl:.2f}", today))
            self.conn.commit()
            log.critical(f"Circuit breaker tripped! Daily P&L: ${total_pnl:.2f}")
            return True
        return False

    def reset(self):
        """Reset the circuit breaker for today (manual override)."""
        today = date.today().isoformat()
        self.conn.execute("""
            UPDATE circuit_breaker SET
                is_tripped = 0,
                tripped_at = NULL,
                reason = NULL
            WHERE date = ?
        """, (today,))
        self.conn.commit()
        log.info("Circuit breaker reset for today")

    def get_status(self):
        """Get current circuit breaker status."""
        today = date.today().isoformat()
        row = self.conn.execute("""
            SELECT is_tripped, manual_kill, daily_realized_pnl, daily_unrealized_pnl,
                total_daily_pnl, tripped_at, reason
            FROM circuit_breaker WHERE date = ?
        """, (today,)).fetchone()
        if row:
            return {
                "is_tripped": bool(row[0]),
                "manual_kill": bool(row[1]),
                "daily_realized_pnl": row[2],
                "daily_unrealized_pnl": row[3],
                "total_daily_pnl": row[4],
                "tripped_at": row[5],
                "reason": row[6],
            }
        return None
