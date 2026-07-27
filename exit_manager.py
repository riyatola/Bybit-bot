"""
Exit manager: monitors open positions for stop-loss/take-profit and
generates approval requests for exits. Adapted for crypto.
"""

import logging
import json
from datetime import date, datetime

from strategies.base import dte, parse_bybit_symbol

log = logging.getLogger("exit_manager")

_OPEN_INSTRUCTION = {"BUY_TO_OPEN", "SELL_TO_OPEN"}
_CLOSE_INSTRUCTION = {
    "BUY_TO_OPEN": "SELL_TO_CLOSE",
    "SELL_TO_OPEN": "BUY_TO_CLOSE",
    "BUY_TO_CLOSE": "BUY_TO_CLOSE",
    "SELL_TO_CLOSE": "SELL_TO_CLOSE",
}


def build_exit_order_payload(candidate: dict) -> dict:
    """Schwab-shaped close order from an exit candidate built by ExitManager."""
    qty = candidate.get("suggested_qty", 1)
    legs = candidate.get("legs") or []
    order_legs = []
    for leg in legs:
        instr = leg.get("instruction", "SELL_TO_CLOSE")
        order_legs.append({
            "instruction": instr,
            "quantity": qty * leg.get("ratio", 1),
            "instrument": {
                "symbol": leg["occ_symbol"],
                "assetType": "OPTION",
            },
        })

    # Closing a credit position usually costs debit; closing debit may yield credit.
    order_type = "NET_DEBIT"
    if candidate.get("is_credit") and candidate.get("est_price", 0) <= 0:
        order_type = "NET_CREDIT"

    return {
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": f"{abs(candidate.get('est_price', 0)):.2f}",
        "orderLegCollection": order_legs,
    }


class ExitManager:
    def __init__(self, cfg: dict, db, client, account_hash: str, market_data=None, sentiment=None):
        self.cfg = cfg
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        self.market_data = market_data
        self.sentiment = sentiment
        self.exit_cfg = self.cfg.get("exits", {})
        self.enabled = self.exit_cfg.get("enabled", True)
        self.last_risk_off_alert_date = None

    def get_open_trades(self):
        # same as original
        return self.conn.execute(
            "SELECT id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy, expiration "
            "FROM trades WHERE status='EXECUTED' AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()

    def calculate_position_mtm(self, order_json: str):
        """Calculate current mark-to-market price of the position using real market data."""
        legs = json.loads(order_json).get("orderLegCollection", [])
        if not legs:
            return None

        mtm = 0.0
        for leg in legs:
            instr = leg.get("instrument", {})
            bybit_sym = instr.get("symbol")
            if not bybit_sym:
                continue

            try:
                parsed = parse_bybit_symbol(bybit_sym)
            except ValueError:
                log.warning(f"Couldn't parse Bybit symbol {bybit_sym}")
                continue

            # Try real market data first
            current_price = None
            if self.market_data:
                try:
                    # Get chain for that underlying and expiration
                    chain = self.market_data.get_option_chain(
                        parsed["underlying"],
                        dte_min=0,
                        dte_max=400
                    )
                    # Find the contract in the chain
                    for exp, sides in chain.items():
                        if exp != parsed["expiration"]:
                            continue
                        bucket = "calls" if parsed["option_type"] == "call" else "puts"
                        for c in sides.get(bucket, []):
                            if abs(c["strike"] - parsed["strike"]) < 0.01:
                                current_price = (c["bid"] + c["ask"]) / 2
                                break
                        if current_price:
                            break
                except Exception:
                    log.debug(f"Couldn't get real quote for {bybit_sym}")

            # Fall back to synthetic pricing? For crypto, we could use Black-Scholes, but we'll skip for simplicity.
            # For paper mode, the paper broker already stores entry price; we could use that as fallback.
            if current_price is None:
                # Use entry price as fallback (not ideal but better than nothing)
                current_price = leg.get("averagePrice", 0)

            if current_price:
                instr = leg.get("instruction", "")
                qty = leg.get("quantity", 0)
                sign = 1 if instr.startswith("BUY") else -1
                mtm += sign * current_price * (qty / 1)  # multiplier 1

        return round(mtm, 2) if mtm else None

    def check_triggers(self) -> list[dict]:
        """Return exit triggers for open trades hitting stop, target, or DTE floor."""
        if not self.enabled:
            return []

        triggers = []
        force_dte = self.exit_cfg.get("force_exit_dte", 7)
        today = date.today().isoformat()

        for row in self.get_open_trades():
            trade_id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy, expiration = row

            pending = self.conn.execute(
                "SELECT exit_approval_id FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if pending and pending[0]:
                approval = self.db.get_approval(pending[0])
                if approval and approval["status"] == "PENDING":
                    continue

            mtm = self.calculate_position_mtm(order_json)
            if mtm is None:
                continue

            entry_price = float(entry_price or 0)
            is_credit = bool(is_credit)
            trigger_type = None

            if is_credit:
                if stop_loss is not None and mtm >= float(stop_loss):
                    trigger_type = "stop_loss"
                elif take_profit is not None and mtm <= float(take_profit):
                    trigger_type = "take_profit"
            else:
                if stop_loss is not None and mtm <= float(stop_loss):
                    trigger_type = "stop_loss"
                elif take_profit is not None and mtm >= float(take_profit):
                    trigger_type = "take_profit"

            if not trigger_type and expiration:
                try:
                    days_left = dte(expiration)
                    if days_left <= force_dte:
                        trigger_type = "force_exit_dte"
                except Exception:
                    pass

            if not trigger_type and self.sentiment and self.sentiment.cfg.get("enabled", False):
                regime = self.sentiment.current_regime()
                if regime == "RISK_OFF" and self.last_risk_off_alert_date != today:
                    strat_mult = self.sentiment.cfg.get("risk_off_multipliers", {}).get(strategy, 1.0)
                    if strat_mult == 0.0:
                        trigger_type = "risk_off"
                        self.last_risk_off_alert_date = today

            if trigger_type:
                triggers.append({
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "strategy": strategy,
                    "trigger_type": trigger_type,
                    "order_json": order_json,
                    "entry_price": entry_price,
                    "current_mtm": mtm,
                    "is_credit": is_credit,
                    "expiration": expiration,
                })

        return triggers

    def build_exit_candidate(self, trigger: dict) -> dict:
        """Build a Telegram/executor-ready exit candidate from a trigger."""
        order = json.loads(trigger["order_json"])
        legs_in = order.get("orderLegCollection", [])
        exit_legs = []
        summaries = []

        for leg in legs_in:
            open_instr = leg.get("instruction", "")
            close_instr = _CLOSE_INSTRUCTION.get(open_instr, "SELL_TO_CLOSE")
            sym = leg.get("instrument", {}).get("symbol", "")
            exit_legs.append({
                "occ_symbol": sym,
                "instruction": close_instr,
                "ratio": 1,
            })
            side = "Buy" if close_instr.startswith("BUY") else "Sell"
            summaries.append(f"{side} {sym}")

        exp = trigger.get("expiration") or ""
        days = dte(exp) if exp else 0
        mtm = trigger["current_mtm"]
        trigger_type = trigger["trigger_type"]

        return {
            "symbol": trigger["symbol"],
            "strategy": trigger["strategy"],
            "trigger_type": trigger_type,
            "trade_id": trigger["trade_id"],
            "description": (
                f"Close {trigger['strategy']} — {trigger_type.replace('_', ' ')} "
                f"(entry ${trigger['entry_price']:.2f}, MTM ${mtm:.2f})"
            ),
            "legs_summary": " / ".join(summaries),
            "legs": exit_legs,
            "expiration": exp,
            "dte": days,
            "est_price": mtm,
            "entry_price": trigger["entry_price"],
            "is_credit": trigger["is_credit"],
            "suggested_qty": 1,
            "score": 1.0,
            "rationale": f"Automated exit trigger: {trigger_type}",
        }