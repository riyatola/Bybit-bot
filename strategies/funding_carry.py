"""
Strategy E: Funding Rate + Options Carry (Delta-Neutral Yield).

Edge: Bybit perp funding is often persistently positive (longs pay
shorts). Shorting the perp to harvest funding is "free" carry but leaves
you naked short spot — this pairs it with a slightly-OTM long call as a
tail hedge, partly paid for by the funding income. Both legs come back
as ONE candidate: a CRYPTO-asset perp leg and an OPTION leg, ratio 1:1
by default (1 unit of perp notional per 1 call contract).
hedge_manager.py can true the net delta up further afterwards on its
normal schedule.

IMPORTANT: executor.py's build_order_payload must read `asset_type` off
each leg (defaulting to "OPTION") so the perp leg here routes to
Bybit's linear/perp order path instead of the options path. See the
one-line patch called out alongside this module.
"""

import logging
from strategies.base import dte, bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.funding_carry")

DEFAULTS = {
    "enabled": True,
    "min_funding_rate": 0.0001,     # 8h funding rate threshold to bother shorting
    "call_target_delta": 0.30,
    "dte_min": 21,
    "dte_max": 45,
    "max_leg_spread_pct": 0.20,
    "perp_notional_per_call": 1.0,  # coin units of perp shorted per 1 call contract
}


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("funding_carry", {})}
    if not s_cfg["enabled"]:
        return []
    candidates = []

    for symbol in symbols:
        ticker = client.get_linear_ticker(symbol) if hasattr(client, "get_linear_ticker") else None
        funding = float(ticker.get("fundingRate", 0) or 0) if ticker else 0.0
        if funding < s_cfg["min_funding_rate"]:
            continue

        spot = market_data.get_last_price(symbol) or 0.0
        if spot <= 0:
            continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)

        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            call = nearest_by_delta(sides["calls"], s_cfg["call_target_delta"])
            if not call:
                continue

            call_sym = bybit_symbol(symbol, expiration, "C", call["strike"])
            perp_sym = f"{symbol.upper()}USDT"
            perp_qty = round(s_cfg["perp_notional_per_call"], 6)

            # Worst case ≈ premium paid on the call, plus the bounded loss
            # region between the perp's short entry and the call strike
            # (above the strike the call caps further perp losses; below
            # entry the perp is profitable, not a loss).
            bounded_upside_loss = max(0.0, call["strike"] - spot)
            max_loss = round(call["ask"] + bounded_upside_loss, 2)

            candidates.append({
                "symbol": symbol,
                "strategy": "funding_carry",
                "description": f"Short perp + long {call['strike']}C tail hedge, funding {funding:+.4%}",
                "legs_summary": f"Short {perp_qty} {perp_sym} perp / Buy {call['strike']}C",
                "legs": [
                    {
                        "occ_symbol": perp_sym,
                        "instruction": "SELL_TO_OPEN",
                        "ratio": perp_qty,
                        "quantity": perp_qty,
                        "asset_type": "CRYPTO",
                        "delta": -1.0,  # perp is delta -1 per unit shorted
                        "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                    },
                    {
                        "occ_symbol": call_sym,
                        "instruction": "BUY_TO_OPEN",
                        "ratio": 1,
                        "quantity": 1,
                        "asset_type": "OPTION",
                        "delta": call.get("delta", 0.0),
                        "gamma": call.get("gamma", 0.0),
                        "vega": call.get("vega", 0.0),
                        "theta": call.get("theta", 0.0),
                    },
                ],
                "expiration": expiration,
                "dte": days,
                "est_price": call["ask"],
                "is_credit": False,
                "max_loss_per_contract": max_loss,
                "score": round(min(1.0, funding / 0.001 * 0.7 + 0.3), 3),
                "rationale": (f"8h funding {funding:+.4%} on {symbol}USDT, {days}DTE tail hedge "
                              f"at {call.get('delta', 0):.2f} delta. Roll/re-strike the call "
                              f"roughly monthly to keep net delta near zero."),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
