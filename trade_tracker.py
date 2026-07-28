"""
Closes the loop on trades: the existing `trades` table (approval_manager.py)
only ever records status SUBMITTED/FAILED at order-placement time. Nothing
in the original codebase ever finds out whether a trade later won or lost.
Both learning.py and guardrails.py need that outcome, so this module owns
getting it.

Approach:
  - Adds outcome columns to the existing `trades` table (idempotent
    migration — safe to call every startup).
  - On each poll, for every trade still marked EXECUTED with no outcome
    yet, checks whether the option position is still open. If it's gone
    from the account's positions, treats it as closed and pulls the
    realized P&L from Schwab's transaction history for that symbol/order.

CAVEAT: Schwab's transaction-history endpoint/field names
(`/accounts/{accountHash}/transactions`) should be double-checked against
the current Trader API reference before relying on this — same caveat
that already applies to executor.py and market_data.py in this repo.
This module degrades gracefully: if it can't find a matching transaction,
it still marks the trade CLOSED_UNKNOWN_PNL (so it stops being retried
forever) rather than guessing a P&L.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger("trade_tracker")


def ensure_trade_outcome_columns(conn: sqlite3.Connection):
    """Idempotent migration. ALTER TABLE has no IF NOT EXISTS in sqlite,
    so we just swallow the 'duplicate column' error."""
    for stmt in (
        "ALTER TABLE trades ADD COLUMN realized_pnl REAL",
        "ALTER TABLE trades ADD COLUMN outcome TEXT",          # WIN / LOSS / BREAKEVEN / OPEN / CLOSED_UNKNOWN_PNL
        "ALTER TABLE trades ADD COLUMN closed_at TEXT",
        "ALTER TABLE trades ADD COLUMN strategy TEXT",
        "ALTER TABLE trades ADD COLUMN score_at_entry REAL",
        "ALTER TABLE trades ADD COLUMN features_json TEXT",    # snapshot of candidate feature inputs, for ML training
        "ALTER TABLE trades ADD COLUMN entry_price REAL",   # entry price (credit/debit)
        "ALTER TABLE trades ADD COLUMN stop_loss REAL",      # stop loss level (absolute price)
        "ALTER TABLE trades ADD COLUMN take_profit REAL",   # take profit level (absolute price)
        "ALTER TABLE trades ADD COLUMN is_credit BOOLEAN", # whether this was a credit trade
        "ALTER TABLE trades ADD COLUMN expiration TEXT",     # expiration date of the position
        "ALTER TABLE trades ADD COLUMN exit_approval_id TEXT", # if we generated an exit approval
        "ALTER TABLE trades ADD COLUMN entry_greeks_json TEXT", # PortfolioGreeks-per-contract + contracts, for portfolio aggregation
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


class TradeTracker:
    def __init__(self, db, client, account_hash: str):
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        ensure_trade_outcome_columns(self.conn)

    def record_entry_context(self, trade_id: str, candidate: dict, stop_loss: float = None, take_profit: float = None):
        """Call right after record_trade() so we have strategy/score/features
        for learning later, even before the trade closes."""
        import json
        greeks_payload = None
        if isinstance(candidate.get("_entry_greeks"), dict):
            greeks_payload = json.dumps(candidate["_entry_greeks"])
        elif isinstance(candidate.get("_entry_greeks_json"), str):
            greeks_payload = candidate["_entry_greeks_json"]
        self.conn.execute(
            "UPDATE trades SET strategy=?, score_at_entry=?, features_json=?, outcome='OPEN', "
            "entry_price=?, stop_loss=?, take_profit=?, is_credit=?, expiration=?, "
            "entry_greeks_json=? WHERE id=?",
            (candidate.get("strategy"), candidate.get("score"),
             json.dumps(_extract_features(candidate)),
             candidate.get("est_price"), stop_loss, take_profit,
             candidate.get("is_credit"), candidate.get("expiration"),
             greeks_payload, trade_id),
        )
        self.conn.commit()

    def poll_and_update_outcomes(self, lookback_days: int = 120):
        """Run this on a schedule (e.g. every 30-60 min during market hours).
        Finds EXECUTED trades with outcome OPEN, checks if still held, and
        records the outcome once they've closed."""
        open_trades = self.conn.execute(
            "SELECT id, symbol, order_json, schwab_order_id, created_at FROM trades "
            "WHERE status='EXECUTED' AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()
        if not open_trades:
            return 0

        try:
            held_symbols = self._current_option_symbols()
        except Exception as e:
            log.warning("Couldn't fetch current positions, skipping outcome poll: %s", e)
            return 0

        updated = 0
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        for trade_id, symbol, order_json, schwab_order_id, created_at in open_trades:
            try:
                created = datetime.fromisoformat(created_at)
            except ValueError:
                created = None
            if created and created < cutoff:
                # too old to still be a live position we'd expect to see; give up cleanly
                self._mark_outcome(trade_id, None, "CLOSED_UNKNOWN_PNL")
                updated += 1
                continue

            import json
            occ_symbols = {leg["instrument"]["symbol"] for leg in json.loads(order_json).get("orderLegCollection", [])}
            if occ_symbols & held_symbols:
                continue  # still open, nothing to do yet

            pnl = self._lookup_realized_pnl(schwab_order_id, symbol)
            outcome = "CLOSED_UNKNOWN_PNL"
            if pnl is not None:
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
            self._mark_outcome(trade_id, pnl, outcome)
            updated += 1

        if updated:
            log.info("Updated outcomes for %d trade(s)", updated)
        return updated

    def _mark_outcome(self, trade_id, pnl, outcome):
        self.conn.execute(
            "UPDATE trades SET realized_pnl=?, outcome=?, closed_at=? WHERE id=?",
            (pnl, outcome, datetime.utcnow().isoformat(), trade_id),
        )
        self.conn.commit()

    def _current_option_symbols(self) -> set:
        resp = self.client.account_details(self.account_hash, fields="positions")
        resp.raise_for_status()
        data = resp.json()["securitiesAccount"]
        out = set()
        for p in data.get("positions", []):
            instr = p.get("instrument", {})
            if instr.get("assetType") == "OPTION" and instr.get("symbol"):
                out.add(instr["symbol"])
        return out

    def _lookup_realized_pnl(self, schwab_order_id: str, symbol: str):
        """Best-effort: scan recent transactions for closing trades on this
        symbol and sum realized P&L. Returns None if nothing matches (caller
        records CLOSED_UNKNOWN_PNL rather than a fabricated number)."""
        try:
            resp = self.client.transactions(
                self.account_hash, symbol=symbol, types="TRADE",
            )
            resp.raise_for_status()
            txns = resp.json()
        except Exception as e:
            log.warning("Transaction lookup failed for %s (order %s): %s", symbol, schwab_order_id, e)
            return None

        total = 0.0
        found = False
        for t in txns if isinstance(txns, list) else txns.get("transactions", []):
            # Field name is a best guess (netAmount / realizedGain style fields
            # vary by Schwab API version) — verify against current docs.
            amount = t.get("netAmount")
            if amount is not None:
                total += float(amount)
                found = True
        return total if found else None


def _extract_features(candidate: dict) -> dict:
    """Pulls a flat numeric feature vector out of a candidate dict for later
    ML training. Only includes fields that are safe to be missing (some
    strategies don't populate all of them)."""
    return {
        "strategy": candidate.get("strategy"),
        "score": candidate.get("score"),
        "dte": candidate.get("dte"),
        "est_price": candidate.get("est_price"),
        "max_loss_per_contract": candidate.get("max_loss_per_contract"),
        "is_credit": candidate.get("is_credit"),
    }
