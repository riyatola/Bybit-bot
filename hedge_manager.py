"""
DeltaHedger: keeps the book delta-flat using perp market orders.

Several strategies (vrp_strangle, calendar_spread, risk_reversal,
event_straddle, funding_carry) are deliberately written as *unhedged*
option candidates — each scanner's job is picking a good option trade,
not managing the resulting delta minute-to-minute. This module is the
other half. On its own schedule (default every 4 hours) it:

  1. Sums net option delta per underlying across all open EXECUTED
     positions, re-pricing each leg's current delta from the live
     option chain (not the stale entry-time delta).
  2. Nets that against any existing perp position for that underlying.
  3. If the net exceeds the configured threshold (0.05 units by
     default), submits a perp market order sized to flatten it back
     toward zero.

Design choice: hedge orders bypass the Telegram approval queue and
execute directly. That's intentional — "re-hedge every 4 hours" is a
mechanical, risk-REDUCING action, not a new trade idea a human needs to
bless, and requiring a tap every 4 hours defeats the point of automated
delta hedging. If you'd rather route hedges through approval_manager
too, swap client.place_order(...) below for
db.create_approval(...) + notifier.send_approval_request(...).

Reuses the same leg schema (asset_type="CRYPTO") that funding_carry.py
introduced, so both BybitClient and PaperBrokerClient fill it consistently.
"""

import json
import logging

log = logging.getLogger("hedge_manager")

DEFAULTS = {
    "enabled": True,
    "rehedge_threshold_units": 0.05,   # re-hedge once |net delta| exceeds this many units of underlying
    "rehedge_interval_seconds": 4 * 3600,
}


class DeltaHedger:
    def __init__(self, cfg: dict, db, client, account_hash: str, market_data):
        self.cfg = {**DEFAULTS, **cfg.get("hedge_manager", {})}
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        self.market_data = market_data

    # ---------- data gathering ----------

    def _open_option_legs_by_underlying(self) -> dict:
        """underlying -> list of (parsed_option_dict, signed_contract_qty)."""
        from strategies.base import parse_bybit_symbol

        out: dict = {}
        rows = self.conn.execute(
            "SELECT order_json FROM trades WHERE status='EXECUTED' "
            "AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()
        for (order_json,) in rows:
            if not order_json:
                continue
            try:
                legs = json.loads(order_json).get("orderLegCollection", [])
            except (TypeError, ValueError):
                continue
            for leg in legs:
                instr = leg.get("instrument", {})
                if instr.get("assetType") != "OPTION":
                    continue
                sym = instr.get("symbol", "")
                try:
                    parsed = parse_bybit_symbol(sym)
                except ValueError:
                    continue
                qty = int(leg.get("quantity", 0) or 0)
                sign = 1 if str(leg.get("instruction", "")).startswith("BUY") else -1
                out.setdefault(parsed["underlying"].upper(), []).append((parsed, sign * qty))
        return out

    def _net_option_delta(self, underlying: str, positions: list) -> float:
        total_delta = 0.0
        chain = self.market_data.get_option_chain(underlying, dte_min=0, dte_max=400)
        for parsed, signed_qty in positions:
            sides = chain.get(parsed["expiration"], {})
            bucket = "calls" if parsed["option_type"] == "call" else "puts"
            opt = next((o for o in sides.get(bucket, []) if abs(o["strike"] - parsed["strike"]) < 0.01), None)
            if opt is None:
                continue
            total_delta += opt.get("delta", 0.0) * signed_qty
        return total_delta

    def _current_perp_position(self, underlying: str) -> float:
        """Net perp position in coin units (positive = long), best-effort."""
        if hasattr(self.client, "_fetch_derivative_positions"):
            try:
                for p in self.client._fetch_derivative_positions():
                    if p.get("instrument", {}).get("symbol", "").upper() == underlying.upper():
                        return float(p.get("longQuantity", 0) or 0) - float(p.get("shortQuantity", 0) or 0)
            except Exception:
                log.debug("hedge_manager: couldn't read live perp position for %s", underlying)
        return 0.0

    # ---------- main entrypoint (call from scanner.py on a schedule) ----------

    async def rehedge_all(self):
        if not self.cfg["enabled"]:
            return
        by_underlying = self._open_option_legs_by_underlying()
        if not by_underlying:
            log.debug("hedge_manager: no open option positions, nothing to hedge")
            return

        threshold = self.cfg["rehedge_threshold_units"]
        for underlying, positions in by_underlying.items():
            option_delta = self._net_option_delta(underlying, positions)
            perp_position = self._current_perp_position(underlying)
            net_delta = option_delta + perp_position

            if abs(net_delta) <= threshold:
                continue

            hedge_qty = round(-net_delta, 6)  # trade this much perp to flatten to ~0
            instruction = "BUY_TO_OPEN" if hedge_qty > 0 else "SELL_TO_OPEN"
            payload = {
                "orderType": "MARKET",
                "session": "NORMAL",
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "price": "0",
                "orderLegCollection": [{
                    "instruction": instruction,
                    "quantity": abs(hedge_qty),
                    "instrument": {"symbol": f"{underlying.upper()}USDT", "assetType": "CRYPTO"},
                }],
            }
            log.info("hedge_manager: %s net delta %+.4f (options %+.4f, perp %+.4f) — hedging %s %.4f",
                      underlying, net_delta, option_delta, perp_position, instruction, abs(hedge_qty))
            try:
                resp = self.client.place_order(self.account_hash, payload)
                if resp.status_code not in (200, 201):
                    log.warning("hedge_manager: hedge order rejected for %s: %s", underlying, resp.text)
            except Exception:
                log.exception("hedge_manager: failed to submit hedge order for %s", underlying)
