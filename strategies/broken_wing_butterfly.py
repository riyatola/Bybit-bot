"""
Strategy F: Broken Wing Butterflies (Asymmetric Directional).

Bullish BWB (calls): buy 1x lower strike, sell 2x middle strike, buy 1x
upper strike, where the upper wing is deliberately WIDER than the lower
wing so the trade opens flat or for a small credit — unlike a standard
butterfly, a runaway move through the upper wing still profits (or at
worst scratches) instead of capping out. Bearish BWB mirrors this with
puts (wide wing to the downside).

max_loss_per_contract is computed exactly (not heuristically) by
evaluating the intrinsic-value payoff of the 3-leg combo at every kink
point (strike) plus a point far past the wide wing, since the combo's
net long/short quantity is always zero (1 - 2 + 1), so the payoff is
guaranteed to flatten out beyond the last strike.
"""

import logging
from strategies.base import dte, bybit_symbol, filter_liquid_chain

log = logging.getLogger("strategies.broken_wing_butterfly")

DEFAULTS = {
    "enabled": True,
    "direction": "bullish",       # "bullish" (calls) or "bearish" (puts)
    "middle_offset_pct": 0.05,    # middle strike = spot * (1 +/- offset)
    "narrow_wing_pct": 0.05,      # distance from middle to the narrow (paid) wing
    "wide_wing_pct": 0.10,        # distance from middle to the broken (wide) wing
    "dte_min": 14,
    "dte_max": 45,
    "max_leg_spread_pct": 0.20,
    "max_net_debit_dollars": 300.0,
}


def _closest_strike(options: list[dict], target: float):
    return min(options, key=lambda o: abs(o["strike"] - target), default=None)


def _payoff_at(strikes_qty: list[tuple[float, int]], price: float) -> float:
    """Intrinsic-value payoff (call-style) of a set of (strike, signed_qty)
    legs at expiration price `price` (signed_qty positive = long)."""
    return sum(qty * max(price - strike, 0.0) for strike, qty in strikes_qty)


def _leg(opt, occ_symbol, instruction, qty):
    return {
        "occ_symbol": occ_symbol, "instruction": instruction,
        "ratio": qty, "quantity": qty,
        "delta": opt.get("delta", 0.0), "gamma": opt.get("gamma", 0.0),
        "vega": opt.get("vega", 0.0), "theta": opt.get("theta", 0.0),
    }


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("broken_wing_butterfly", {})}
    if not s_cfg["enabled"]:
        return []
    candidates = []
    bullish = s_cfg["direction"] == "bullish"
    opt_code = "C" if bullish else "P"

    for symbol in symbols:
        spot = market_data.get_last_price(symbol) or 0.0
        if spot <= 0:
            continue
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)

        if bullish:
            middle_target = spot * (1 + s_cfg["middle_offset_pct"])
            narrow_target = middle_target * (1 - s_cfg["narrow_wing_pct"])
            wide_target = middle_target * (1 + s_cfg["wide_wing_pct"])
        else:
            middle_target = spot * (1 - s_cfg["middle_offset_pct"])
            narrow_target = middle_target * (1 + s_cfg["narrow_wing_pct"])
            wide_target = middle_target * (1 - s_cfg["wide_wing_pct"])

        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            options = sides["calls"] if bullish else sides["puts"]
            if len(options) < 3:
                continue

            lower = _closest_strike(options, min(narrow_target, wide_target))
            middle = _closest_strike(options, middle_target)
            upper = _closest_strike(options, max(narrow_target, wide_target))
            if not (lower and middle and upper):
                continue
            strikes = sorted({lower["strike"], middle["strike"], upper["strike"]})
            if len(strikes) != 3:
                continue
            lo_k, mid_k, hi_k = strikes
            lo_opt = next(o for o in options if o["strike"] == lo_k)
            mid_opt = next(o for o in options if o["strike"] == mid_k)
            hi_opt = next(o for o in options if o["strike"] == hi_k)

            net_debit = round(lo_opt["ask"] - 2 * mid_opt["bid"] + hi_opt["ask"], 2)
            if net_debit > s_cfg["max_net_debit_dollars"]:
                continue

            strikes_qty = [(lo_k, 1), (mid_k, -2), (hi_k, 1)]
            kink_points = [0.0, lo_k, mid_k, hi_k, hi_k * 1.5]
            worst_payoff = min(_payoff_at(strikes_qty, p) - net_debit for p in kink_points)
            max_loss = round(max(0.0, -worst_payoff), 2)

            lo_sym = bybit_symbol(symbol, expiration, opt_code, lo_k)
            mid_sym = bybit_symbol(symbol, expiration, opt_code, mid_k)
            hi_sym = bybit_symbol(symbol, expiration, opt_code, hi_k)

            legs = [
                _leg(lo_opt, lo_sym, "BUY_TO_OPEN", 1),
                _leg(mid_opt, mid_sym, "SELL_TO_OPEN", 2),
                _leg(hi_opt, hi_sym, "BUY_TO_OPEN", 1),
            ]

            direction_label = "Bullish" if bullish else "Bearish"
            wide_strike = hi_k if bullish else lo_k
            candidates.append({
                "symbol": symbol,
                "strategy": "broken_wing_butterfly",
                "description": f"{direction_label} BWB @ {lo_k}/{mid_k}/{hi_k}",
                "legs_summary": f"Buy {lo_k}{opt_code} / Sell 2x {mid_k}{opt_code} / Buy {hi_k}{opt_code}",
                "legs": legs,
                "expiration": expiration,
                "dte": days,
                "est_price": net_debit,
                "is_credit": net_debit < 0,
                "max_loss_per_contract": max_loss,
                "score": round(max(0.0, 1.0 - max_loss / max(mid_k - lo_k, 1.0) / 3.0), 3),
                "rationale": (f"Net {'credit' if net_debit < 0 else 'debit'} ${abs(net_debit):.2f}, "
                              f"max defined loss ${max_loss:.2f}, max profit near {mid_k}. "
                              f"Wide wing at {wide_strike} keeps a runaway move profitable "
                              f"instead of capping out like a standard butterfly."),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
