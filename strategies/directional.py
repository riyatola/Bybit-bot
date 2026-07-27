"""
Directional (long call/put) scanner for crypto options.
Uses trend + volume breakout; IV rank window to avoid expensive vol.
"""

import logging
from strategies.base import dte, bybit_symbol, parse_bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.directional")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = cfg["strategies"]["directional"]
    candidates = []

    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        if not (s_cfg["min_iv_rank"] <= rank <= s_cfg["max_iv_rank"]):
            continue

        vol_ratio = market_data.get_volume_ratio(symbol)
        if vol_ratio < s_cfg["volume_multiple_trigger"]:
            continue

        trend_strength = market_data.get_trend_strength(symbol, s_cfg["trend_lookback_days"])
        if abs(trend_strength) < 0.2:
            continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            option_type = "call" if trend_strength > 0 else "put"
            options = sides["calls"] if option_type == "call" else sides["puts"]
            leg = nearest_by_delta(options, 0.40)
            if not leg:
                continue

            mid = round((leg["bid"] + leg["ask"]) / 2, 2)
            score = min(1.0, abs(trend_strength) * 0.6 + min(vol_ratio / 3, 1.0) * 0.4)

            candidates.append({
                "symbol": symbol,
                "strategy": "directional",
                "description": f"Long {option_type} on breakout, trend strength {trend_strength:+.2f}",
                "legs_summary": f"Buy {leg['strike']}{option_type[0].upper()}",
                "legs": [{
                    "occ_symbol": bybit_symbol(symbol, expiration, option_type[0], leg["strike"]),
                    "instruction": "BUY_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": mid,
                "is_credit": False,
                "max_loss_per_contract": round(mid * 1, 2),  # multiplier=1
                "score": score,
                "rationale": (f"Volume {vol_ratio:.1f}x avg, trend {trend_strength:+.2f}, "
                              f"IV rank {rank:.0f} (in acceptable range)"),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates