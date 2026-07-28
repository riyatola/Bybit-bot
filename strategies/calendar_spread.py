"""
Strategy B: Volatility Term Structure Calendar Spread.

Edge: crypto IV term structure is often steeply contango (far-dated IV
richer than near-dated IV, priced for tail risk). Sell the front-month
option, buy the back-month option at the same strike — theta decay on
the short leg outruns the long leg while volatility curvature does the
rest. Delta is left to hedge_manager.py, same split as vrp_strangle.py.
"""

import logging
from strategies.base import dte, bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.calendar_spread")

DEFAULTS = {
    "enabled": True,
    "near_dte_target": 7,
    "near_dte_tolerance": 4,
    "far_dte_target": 21,
    "far_dte_tolerance": 7,
    "atm_delta_target": 0.50,
    "max_leg_spread_pct": 0.20,
    "use_straddle": False,   # if True, also build a put calendar (double calendar)
    "max_net_debit_dollars": 500.0,
}


def _closest_expiration(chain: dict, target_dte: int, tolerance: int, exclude=None) -> str | None:
    best, best_diff = None, None
    for expiration in chain:
        if expiration == exclude:
            continue
        diff = abs(dte(expiration) - target_dte)
        if diff > tolerance:
            continue
        if best is None or diff < best_diff:
            best, best_diff = expiration, diff
    return best


def _leg(option: dict, occ_symbol: str, instruction: str) -> dict:
    return {
        "occ_symbol": occ_symbol,
        "instruction": instruction,
        "ratio": 1,
        "quantity": 1,
        "delta": option.get("delta", 0.0),
        "gamma": option.get("gamma", 0.0),
        "vega": option.get("vega", 0.0),
        "theta": option.get("theta", 0.0),
    }


def _build_calendar(symbol, near_exp, near_days, near_side, far_exp, far_days, far_side,
                     option_type_code, s_cfg):
    target = s_cfg["atm_delta_target"] if option_type_code == "C" else -s_cfg["atm_delta_target"]
    near_opt = nearest_by_delta(near_side, target)
    if not near_opt:
        return None
    far_opt = min(far_side, key=lambda o: abs(o["strike"] - near_opt["strike"]), default=None)
    if not far_opt or abs(far_opt["strike"] - near_opt["strike"]) > near_opt["strike"] * 0.01:
        return None

    near_sym = bybit_symbol(symbol, near_exp, option_type_code, near_opt["strike"])
    far_sym = bybit_symbol(symbol, far_exp, option_type_code, far_opt["strike"])

    net_debit = round(far_opt["ask"] - near_opt["bid"], 2)
    if net_debit <= 0 or net_debit > s_cfg["max_net_debit_dollars"]:
        return None

    label = "Call" if option_type_code == "C" else "Put"
    steepness = (far_opt["mark"] - near_opt["mark"]) / max(far_opt["mark"], 0.01)
    score = 0.5 + min(max(steepness, 0), 0.5) * 0.5

    return {
        "symbol": symbol,
        "strategy": "calendar_spread",
        "description": f"{label} calendar: sell {near_days}DTE / buy {far_days}DTE @ {near_opt['strike']}",
        "legs_summary": f"Sell {near_days}DTE {near_opt['strike']}{option_type_code} / "
                         f"Buy {far_days}DTE {far_opt['strike']}{option_type_code}",
        "legs": [
            _leg(near_opt, near_sym, "SELL_TO_OPEN"),
            _leg(far_opt, far_sym, "BUY_TO_OPEN"),
        ],
        "expiration": near_exp,   # the leg that needs management first
        "dte": near_days,
        "est_price": net_debit,
        "is_credit": False,
        "max_loss_per_contract": net_debit,  # net debit paid ≈ standard calendar max-loss heuristic
        "score": round(score, 3),
        "rationale": (f"Term structure calendar: near mark ${near_opt['mark']:.2f} vs "
                      f"far mark ${far_opt['mark']:.2f}, net debit ${net_debit:.2f}. "
                      f"Unhedged delta — see hedge_manager.py."),
    }


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("calendar_spread", {})}
    if not s_cfg["enabled"]:
        return []
    candidates = []

    dte_min = max(1, s_cfg["near_dte_target"] - s_cfg["near_dte_tolerance"])
    dte_max = s_cfg["far_dte_target"] + s_cfg["far_dte_tolerance"]

    for symbol in symbols:
        chain = market_data.get_option_chain(symbol, dte_min, dte_max)
        if len(chain) < 2:
            continue
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        chain = {exp: filter_liquid_chain(sides, max_spread_pct) for exp, sides in chain.items()}

        near_exp = _closest_expiration(chain, s_cfg["near_dte_target"], s_cfg["near_dte_tolerance"])
        if not near_exp:
            continue
        far_exp = _closest_expiration(chain, s_cfg["far_dte_target"], s_cfg["far_dte_tolerance"], exclude=near_exp)
        if not far_exp:
            continue

        near_days, far_days = dte(near_exp), dte(far_exp)
        if far_days <= near_days:
            continue

        near_sides, far_sides = chain[near_exp], chain[far_exp]

        call_cal = _build_calendar(symbol, near_exp, near_days, near_sides["calls"],
                                    far_exp, far_days, far_sides["calls"], "C", s_cfg)
        if call_cal:
            candidates.append(call_cal)

        if s_cfg["use_straddle"]:
            put_cal = _build_calendar(symbol, near_exp, near_days, near_sides["puts"],
                                       far_exp, far_days, far_sides["puts"], "P", s_cfg)
            if put_cal:
                candidates.append(put_cal)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
