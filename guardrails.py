"""
Self-imposed guardrails: bans a symbol from future scans if its realized
track record with this bot turns bad. This is separate from
universe.exclude_symbols (which is a static, human-curated blocklist) —
banned_symbols is a *dynamic* table the bot maintains itself based on
outcomes recorded by trade_tracker.py.

Ban triggers (all configurable in config.yaml under `guardrails:`):
  - max_consecutive_losses: N losing trades in a row on that symbol
  - min_win_rate_after: once a symbol has >= min_trades_for_stats closed
    trades, ban if win rate drops below min_win_rate
  - max_cumulative_loss_dollars: total realized P&L on that symbol drops
    below -X

Bans are logged with a reason and are NOT silently permanent — they
expire after `guardrails.ban_duration_days` (default 90) unless
re-triggered, and can always be lifted manually via unban(). This avoids
the bot permanently blacklisting a name off a small unlucky sample.

This only bans by *symbol*, not by strategy — if PLTR is banned it's
banned across credit_spreads/directional/earnings_vol/wheel alike. That's
a deliberate simplification; per-(symbol, strategy) banning is a
reasonable future improvement if you want finer granularity.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger("guardrails")

SCHEMA = """
CREATE TABLE IF NOT EXISTS banned_symbols (
    symbol TEXT,
    strategy TEXT,
    reason TEXT,
    source TEXT,              -- 'auto' or 'manual'
    banned_at TEXT,
    expires_at TEXT,            -- NULL = manual ban, never auto-expires
    PRIMARY KEY (symbol, strategy)
);
"""

DEFAULTS = {
    "enabled": True,
    "min_trades_for_stats": 5,
    "max_consecutive_losses": 3,
    "min_win_rate": 0.30,
    "max_cumulative_loss_dollars": 500.0,
    "ban_duration_days": 90,
}


class Guardrails:
    def __init__(self, cfg: dict, db):
        self.cfg = {**DEFAULTS, **cfg.get("guardrails", {})}
        self.db = db
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- query ----------

    def is_banned(self, symbol: str, strategy: str) -> bool:
        row = self.conn.execute(
            "SELECT expires_at FROM banned_symbols WHERE symbol=? AND strategy=?", (symbol.upper(), strategy)
        ).fetchone()
        if not row:
            return False
        expires_at = row[0]
        if expires_at and datetime.utcnow().isoformat() > expires_at:
            self.unban(symbol, reason="ban expired")
            return False
        return True

    def list_active_bans(self) -> list[dict]:
        self._expire_stale()
        rows = self.conn.execute(
            "SELECT symbol, reason, source, banned_at, expires_at FROM banned_symbols"
        ).fetchall()
        return [
            {"symbol": r[0], "reason": r[1], "source": r[2], "banned_at": r[3], "expires_at": r[4]}
            for r in rows
        ]

    # ---------- mutate ----------

    def ban(self, symbol: str, strategy: str, reason: str, source: str = "auto"):
        symbol = symbol.upper()
        now = datetime.utcnow()
        expires = None
        if source == "auto" and self.cfg["ban_duration_days"]:
            expires = (now + timedelta(days=self.cfg["ban_duration_days"])).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO banned_symbols (symbol, strategy, reason, source, banned_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, strategy, reason, source, now.isoformat(), expires),
        )
        self.conn.commit()
        log.warning("BANNED %s (%s): %s", symbol, source, reason)

    def unban(self, symbol: str, strategy: str, reason: str = "manual unban"):
        self.conn.execute("DELETE FROM banned_symbols WHERE symbol=? AND strategy=?", (symbol.upper(), strategy))
        self.conn.commit()
        log.info("Unbanned %s (%s)", symbol.upper(), reason)

    def _expire_stale(self):
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "DELETE FROM banned_symbols WHERE source='auto' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        self.conn.commit()

    # ---------- evaluation, run periodically ----------

    def evaluate_all_symbols(self):
        """Scans closed-trade history for every (symbol, strategy) pair that has any closed
        trades and applies the ban rules. Call this on a schedule (e.g.
        daily) from scanner.py, not on every scan cycle — it's a summary
        judgment over history, not a per-cycle check."""
        if not self.cfg["enabled"]:
            return []

        symbol_strategy_pairs = self.conn.execute(
            "SELECT DISTINCT symbol, strategy FROM trades WHERE outcome IN ('WIN','LOSS','BREAKEVEN')"
        ).fetchall()

        newly_banned = []
        for symbol, strategy in symbol_strategy_pairs:
            if self.is_banned(symbol, strategy):
                continue
            verdict = self._evaluate_symbol(symbol, strategy)
            if verdict:
                self.ban(symbol, strategy, verdict, source="auto")
                newly_banned.append((symbol, strategy, verdict))
        return newly_banned

    def _evaluate_symbol(self, symbol: str, strategy: str):
        rows = self.conn.execute(
            "SELECT outcome, realized_pnl FROM trades WHERE symbol=? AND strategy=? AND outcome IN ('WIN','LOSS','BREAKEVEN') "
            "ORDER BY closed_at ASC",
            (symbol, strategy),
        ).fetchall()
        if len(rows) < self.cfg["min_trades_for_stats"]:
            return None

        outcomes = [r[0] for r in rows]
        pnls = [r[1] for r in rows if r[1] is not None]

        # consecutive losses at the tail
        consec = 0
        for o in reversed(outcomes):
            if o == "LOSS":
                consec += 1
            else:
                break
        if consec >= self.cfg["max_consecutive_losses"]:
            return f"{consec} consecutive losses"

        wins = sum(1 for o in outcomes if o == "WIN")
        win_rate = wins / len(outcomes)
        if win_rate < self.cfg["min_win_rate"]:
            return f"win rate {win_rate:.0%} over {len(outcomes)} trades (below {self.cfg['min_win_rate']:.0%} floor)"

        if pnls and sum(pnls) <= -abs(self.cfg["max_cumulative_loss_dollars"]):
            return f"cumulative realized P&L ${sum(pnls):.2f} breached loss limit"

        return None
