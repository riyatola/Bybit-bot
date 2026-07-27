"""
Credit spread scanner for crypto options (Bybit).
Adapted from original: uses bybit_symbol/parse_bybit_symbol, filter_liquid_chain.
Assumes put/call spreads on BTC/ETH with European exercise.
"""

import logging
from strategies.base import dte, bybit_symbol, parse_bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.credit_spreads")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = cfg["strategies"]["credit_spreads"]
    candidates = []

    for symbol in symbols:  # symbol is underlying, e.g., BTC
        rank = market_data.get_iv_rank(symbol)
        if rank < s_cfg["min_iv_rank"]:
            continue

        trend = market_data.get_trend(symbol)
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])

        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)

            if trend in ("up", "neutral"):
                candidate = _build_put_credit_spread(symbol, expiration, days, sides["puts"], s_cfg, rank)
                if candidate:
                    candidates.append(candidate)

            if trend in ("down", "neutral"):
                candidate = _build_call_credit_spread(symbol, expiration, days, sides["calls"], s_cfg, rank)
                if candidate:
                    candidates.append(candidate)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _build_put_credit_spread(symbol, expiration, days, puts, s_cfg, rank):
    short = nearest_by_delta(puts, s_cfg["target_short_delta"])
    if not short:
        return None
    long_candidates = [p for p in puts if p["strike"] < short["strike"]]
    if not long_candidates:
        return None
    long_leg = max(long_candidates, key=lambda p: p["strike"])

    width = short["strike"] - long_leg["strike"]
    credit = round((short["bid"] - long_leg["ask"]), 2)
    if width <= 0 or credit <= 0:
        return None
    ratio = credit / width
    if ratio < s_cfg["min_credit_to_width_ratio"]:
        return None

    max_loss = round((width - credit) * 1, 2)  # multiplier = 1 for crypto options
    score = min(1.0, (rank / 100) * 0.6 + min(ratio, 1.0) * 0.4)

    return {
        "symbol": symbol,
        "strategy": "credit_spreads",
        "description": f"Put credit spread, short delta ~{short['delta']:.2f}",
        "legs_summary": f"Sell {short['strike']}P / Buy {long_leg['strike']}P",
        "legs": [
            {"occ_symbol": bybit_symbol(symbol, expiration, "P", short["strike"]),
             "instruction": "SELL_TO_OPEN", "ratio": 1},
            {"occ_symbol": bybit_symbol(symbol, expiration, "P", long_leg["strike"]),
             "instruction": "BUY_TO_OPEN", "ratio": 1},
        ],
        "expiration": expiration,
        "dte": days,
        "est_price": credit,
        "is_credit": True,
        "max_loss_per_contract": max_loss,
        "score": score,
        "rationale": f"IV rank {rank:.0f}, credit/width {ratio:.2f}, short delta {short['delta']:.2f}",
    }


def _build_call_credit_spread(symbol, expiration, days, calls, s_cfg, rank):
    short = nearest_by_delta(calls, s_cfg["target_short_delta"])
    if not short:
        return None
    long_candidates = [c for c in calls if c["strike"] > short["strike"]]
    if not long_candidates:
        return None
    long_leg = min(long_candidates, key=lambda c: c["strike"])

    width = long_leg["strike"] - short["strike"]
    credit = round((short["bid"] - long_leg["ask"]), 2)
    if width <= 0 or credit <= 0:
        return None
    ratio = credit / width
    if ratio < s_cfg["min_credit_to_width_ratio"]:
        return None

    max_loss = round((width - credit) * 1, 2)
    score = min(1.0, (rank / 100) * 0.6 + min(ratio, 1.0) * 0.4)

    return {
        "symbol": symbol,
        "strategy": "credit_spreads",
        "description": f"Call credit spread, short delta ~{short['delta']:.2f}",
        "legs_summary": f"Sell {short['strike']}C / Buy {long_leg['strike']}C",
        "legs": [
            {"occ_symbol": bybit_symbol(symbol, expiration, "C", short["strike"]),
             "instruction": "SELL_TO_OPEN", "ratio": 1},
            {"occ_symbol": bybit_symbol(symbol, expiration, "C", long_leg["strike"]),
             "instruction": "BUY_TO_OPEN", "ratio": 1},
        ],
        "expiration": expiration,
        "dte": days,
        "est_price": credit,
        "is_credit": True,
        "max_loss_per_contract": max_loss,
        "score": score,
        "rationale": f"IV rank {rank:.0f}, credit/width {ratio:.2f}, short delta {short['delta']:.2f}",
    }