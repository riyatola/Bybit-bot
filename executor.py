"""
Translates an approved candidate into a multi-leg order payload and submits it.
This only runs after either (a) the auto-execution gates pass (paper mode), or
(b) a human taps the approve button in Telegram — see notifier.py.

Order payload is intentionally broker-neutral and mirrors the common
"orderLegCollection + NET_DEBIT/NET_CREDIT" schema used by the paper broker.
Bybit live trading (when wired in) will translate the fields from this format
to Bybit's Unified account / v5 place-order payload.
"""

import logging

from exit_manager import build_exit_order_payload

log = logging.getLogger("executor")


def build_order_payload(candidate: dict) -> dict:
    qty = candidate.get("suggested_qty", 1)
    legs = candidate["legs"]  # list of dicts: {symbol (OCC option symbol), instruction, ratio}

    order_legs = []
    for leg in legs:
        order_legs.append({
            "instruction": leg["instruction"],   # BUY_TO_OPEN / SELL_TO_OPEN etc.
            "quantity": qty * leg.get("ratio", 1),
            "instrument": {
                "symbol": leg["occ_symbol"],
                # funding_carry.py / hedge_manager.py attach a perp leg with
                # asset_type="CRYPTO" alongside the option leg(s); default to
                # OPTION for every other strategy's legs, unchanged.
                "assetType": leg.get("asset_type", "OPTION"),
            },
        })

    order_type = "NET_CREDIT" if candidate.get("is_credit") else "NET_DEBIT"

    payload = {
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "DAY",
        # Single-leg and multi-leg (vertical spread, straddle) orders share
        # orderStrategyType=SINGLE: one order object with N legs in
        # orderLegCollection. The paper broker and any live adapter below
        # can fan them out to per-contract Bybit orders if needed.
        "orderStrategyType": "SINGLE",
        "price": f"{candidate['est_price']:.2f}",
        "orderLegCollection": order_legs,
    }
    return payload


class Executor:
    def __init__(self, cfg: dict, client, account_hash: str, db, trade_tracker=None):
        self.cfg = cfg
        self.client = client
        self.account_hash = account_hash
        self.db = db
        # Optional: when wired in (see scanner.py), every executed trade's
        # strategy/score/feature snapshot is recorded so learning.py and
        # guardrails.py have something to learn from once it closes.
        self.trade_tracker = trade_tracker

    async def execute(self, candidate: dict, approval_id: str) -> str:
        # Check if this is an exit order
        if candidate.get("trigger_type"):
            payload = build_exit_order_payload(candidate)
        else:
            payload = build_order_payload(candidate)
        log.info("Submitting order: %s", payload)

        resp = self.client.place_order(self.account_hash, payload)
        strategy = candidate.get("strategy", "unknown")
        if resp.status_code not in (200, 201):
            self.db.record_trade(approval_id, candidate["symbol"], strategy, payload, None, "FAILED")
            raise RuntimeError(f"Order rejected by broker: {resp.status_code} {resp.text}")

        # Paper broker returns order id in body; live adapters should mirror
        # that — parse header Location as a fallback, just in case.
        order_url = resp.headers.get("Location", "")
        order_id = order_url.rsplit("/", 1)[-1] if order_url else (
            getattr(resp, "body", {}).get("id") if hasattr(resp, "body") else "unknown"
        )
        trade_id = self.db.record_trade(approval_id, candidate["symbol"], strategy, payload, order_id, "SUBMITTED")

        if self.trade_tracker and not candidate.get("trigger_type"):
            # Only record entry context for entry trades (not exits)
            try:
                # Get default stop/take from config as percent of entry price
                stop_loss_pct = self.cfg.get("exits", {}).get("default_stop_loss_pct", {}).get(strategy)
                take_profit_pct = self.cfg.get("exits", {}).get("default_take_profit_pct", {}).get(strategy)
                entry_price = candidate.get("est_price")
                
                stop_loss = None
                take_profit = None
                if stop_loss_pct is not None and entry_price:
                    stop_loss = round(entry_price * stop_loss_pct, 4)
                if take_profit_pct is not None and entry_price:
                    take_profit = round(entry_price * take_profit_pct, 4)
                self.trade_tracker.record_entry_context(trade_id, candidate, stop_loss, take_profit)
            except Exception:
                log.exception("Failed to record entry context for trade %s — learning data will be incomplete "
                              "for this trade, but the order itself already went through fine.", trade_id)

        return order_id
