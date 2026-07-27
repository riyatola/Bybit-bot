"""
Wheel strategy scanner for crypto: cash-secured puts (CSP) and covered calls.
Requires a watchlist of underlyings you'd like to own.
"""

import logging
from strategies.base import dte, bybit_symbol, parse_bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.wheel")


def scan(client, account_positions: list[dict], cfg: dict, market_data) -> list[dict]:
    s_cfg = cfg["strategies"]["wheel"]
    watchlist = cfg["universe"].get("wheel_watchlist", [])  # should be defined in config
    candidates = []

    # -- Cash-secured puts --
    max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
    for symbol in watchlist:
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            put = nearest_by_delta(sides["puts"], s_cfg["csp_target_delta"])
            if not put:
                continue
            credit = round((put["bid"] + put["ask"]) / 2, 2)
            # max loss = strike * 1 (contract multiplier) - credit
            max_loss = round(put["strike"] - credit, 2)
            # score based on yield
            score = 0.5 + min(credit / put["strike"], 0.1) * 5
            candidates.append({
                "symbol": symbol,
                "strategy": "wheel",
                "description": f"Cash-secured put, delta ~{put['delta']:.2f}",
                "legs_summary": f"Sell {put['strike']}P",
                "legs": [{
                    "occ_symbol": bybit_symbol(symbol, expiration, "P", put["strike"]),
                    "instruction": "SELL_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": credit,
                "is_credit": True,
                "max_loss_per_contract": max_loss,
                "score": score,
                "rationale": f"Willing-to-own watchlist name, delta {put['delta']:.2f}, "
                             f"annualized yield ~{(credit / put['strike']) * (365 / max(days, 1)):.1%}",
            })

    # -- Covered calls on existing spot positions --
    for pos in account_positions:
        if pos.get("assetType") != "CRYPTO" or pos.get("longQuantity", 0) < 1:
            continue
        symbol = pos["symbol"]  # e.g., BTC
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        cost_basis = pos.get("averagePrice", 0)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            call = nearest_by_delta(sides["calls"], s_cfg["covered_call_target_delta"])
            if not call or call["strike"] < cost_basis:
                continue
            credit = round((call["bid"] + call["ask"]) / 2, 2)
            candidates.append({
                "symbol": symbol,
                "strategy": "wheel",
                "description": f"Covered call, delta ~{call['delta']:.2f}, above cost basis {cost_basis:.2f}",
                "legs_summary": f"Sell {call['strike']}C",
                "legs": [{
                    "occ_symbol": bybit_symbol(symbol, expiration, "C", call["strike"]),
                    "instruction": "SELL_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": credit,
                "is_credit": True,
                "max_loss_per_contract": 0.0,  # covered by spot
                "score": 0.5 + min(credit / call["strike"], 0.1) * 5,
                "rationale": f"Existing {int(pos['longQuantity'])}-unit position, "
                             f"strike above cost basis, delta {call['delta']:.2f}",
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates