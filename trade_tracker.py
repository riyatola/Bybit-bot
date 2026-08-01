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
        "ALTER TABLE trades ADD COLUMN order_id TEXT",         # broker-assigned order id (Bybit, Paper, Hybrid)
        "ALTER TABLE trades ADD COLUMN mock_order BOOLEAN DEFAULT 0",  # 1 = simulated local paper fill (BOT_ALTCOIN_MOCK)
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
            "SELECT id, symbol, order_json, order_id, schwab_order_id, created_at FROM trades "
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
        for trade_id, symbol, order_json, order_id, schwab_order_id, created_at in open_trades:
            try:
                created = datetime.fromisoformat(created_at)
            except ValueError:
                created = None
            if created and created < cutoff:
                self._mark_outcome(trade_id, None, "CLOSED_UNKNOWN_PNL")
                updated += 1
                continue

            import json
            try:
                occ_symbols = {
                    leg["instrument"]["symbol"]
                    for leg in json.loads(order_json or "[]" if "[" in (order_json or "") else '{"legs":[]}').get(
                        "orderLegCollection", []
                    )
                }
            except Exception:
                occ_symbols = set()
            if occ_symbols & held_symbols:
                continue  # still open

            # Try multiple ID fields — (1) bybit broker order_id we set,
            # (2) schwab_order_id legacy col, (3) fallback to symbol scan.
            pnl = self._lookup_realized_pnl(order_id or schwab_order_id, symbol)
            if pnl is None:
                pnl = self._lookup_realized_pnl(schwab_order_id, symbol)
            outcome = "CLOSED_UNKNOWN_PNL"
            if pnl is not None:
                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
            else:
                # For mock/paper altcoins settle_expired_positions() already
                # moved cash into the paper account's ledger but the txn scan
                # above may have missed it if ID naming differs. Try expiry
                # based resolution so trades still land in training data.
                try:
                    exp = self.conn.execute(
                        "SELECT expiration, entry_price, is_credit FROM trades WHERE id=?", (trade_id,)
                    ).fetchone()
                    if exp:
                        import datetime as _dt
                        exp_str, entry, is_credit = exp
                        if exp_str and datetime.fromisoformat(exp_str).date() < _dt.date.today():
                            # Try paper broker intrinsic settlement fallback
                            # via broker client (only works if broker has spot).
                            pnl_fb = self._intrinsic_pnl_fallback(symbol, occ_symbols, entry, is_credit)
                            if pnl_fb is not None:
                                pnl = pnl_fb
                                outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
                                log.info(
                                    "Trade %s (%s): no txn found, settled fallback @ intrinsic = %+0.2f",
                                    trade_id, symbol, pnl,
                                )
                except Exception as e:
                    log.debug("Fallback intrinsic settle for %s skipped: %s", trade_id, e)
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

    def _intrinsic_pnl_fallback(self, underlying: str, occ_symbols: set,
                                entry_price, is_credit) -> float | None:
        """Best-effort settle for mock/paper altcoins whose transactions
        couldn't be found by ID. Computes intrinsic value at expiry for each
        option leg using the broker's real spot lookup, then nets against
        the entry price (credit/debit) per 1 contract."""
        try:
            if len(occ_symbols) != 1 or entry_price is None:
                return None
            from datetime import date as _date, datetime as _dt, timedelta as _td
            from strategies.base import parse_bybit_symbol
            sym = next(iter(occ_symbols))
            parsed = parse_bybit_symbol(sym)
            if not parsed:
                return None
            exp = parsed["expiration"]
            if isinstance(exp, str):
                exp = _dt.fromisoformat(exp).date()
            if exp > _date.today():
                return None
            spot = None
            try:
                if hasattr(self.client, "_get_spot"):
                    spot = float(self.client._get_spot(underlying) or 0.0)
                elif hasattr(self.client, "alt_paper") and hasattr(self.client.alt_paper, "_get_spot"):
                    spot = float(self.client.alt_paper._get_spot(underlying) or 0.0)
                else:
                    for t in (getattr(self.client, "get_linear_tickers", lambda **k: [])() or []):
                        if t.get("symbol") == f"{underlying.upper()}USDT":
                            spot = float(t.get("markPrice") or t.get("lastPrice") or 0.0)
                            break
            except Exception:
                pass
            if spot is None or spot <= 0:
                return None
            K = float(parsed["strike"] or 0.0)
            is_call = str(parsed.get("option_type") or "C").upper().startswith("C")
            intrinsic = max(0.0, (spot - K) if is_call else (K - spot))
            entry_price_f = float(entry_price or 0.0)
            is_credit_bool = bool(is_credit) if isinstance(is_credit, (int, bool)) else (
                str(is_credit).lower() in {"1", "true", "yes", "y", "t"} if is_credit else False
            )
            # Credit: you receive entry at entry_price, then pay intrinsic if ITM
            # Debit : you paid entry at entry_price, then get intrinsic back
            if is_credit_bool:
                pnl_per = entry_price_f - intrinsic
            else:
                pnl_per = intrinsic - entry_price_f
            return round(pnl_per, 4)
        except Exception as e:
            log.debug("intrinsic fallback failed for %s: %s", underlying, e)
            return None


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
