"""
Strategy C: Skew Arbitrage (Risk Reversals).

Edge: crypto put skew goes extreme after crashes (crash protection gets
overpriced); call skew goes extreme during euphoric parabolic runs. This
module fades both: after a sharp drawdown it sells the rich put and buys
the cheap call (long risk reversal); after a sharp rally it does the
opposite (short risk reversal). Uses market_data.get_trend_strength as a
proxy for "how extreme is the recent move" since this bot's chain
doesn't expose a separate skew/IV-by-strike series.
"""

import logging
from strategies.base import dte, bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.risk_reversal")

DEFAULTS = {
    "enabled": True,
    "trend_lookback_days": 10,
    "drawdown_trend_threshold": -0.40,   # trend_strength below this = "sharp drawdown"
    "euphoria_trend_threshold": 0.40,    # trend_strength above this = "parabolic euphoria"
    "target_delta": 0.25,
    "dte_min": 14,
    "dte_max": 45,
    "max_leg_spread_pct": 0.20,
}


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


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("risk_reversal", {})}
    if not s_cfg["enabled"]:
        return []
    candidates = []

    for symbol in symbols:
        strength = market_data.get_trend_strength(symbol, s_cfg["trend_lookback_days"])
        if strength <= s_cfg["drawdown_trend_threshold"]:
            mode = "fade_crash"       # sell rich put, buy cheap call
        elif strength >= s_cfg["euphoria_trend_threshold"]:
            mode = "fade_euphoria"    # sell rich call, buy cheap put
        else:
            continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)

        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            target = s_cfg["target_delta"]
            max_loss = None

            if mode == "fade_crash":
                short_put = nearest_by_delta(sides["puts"], -target)
                long_call = nearest_by_delta(sides["calls"], target)
                if not short_put or not long_call:
                    continue
                net = round(long_call["ask"] - short_put["bid"], 2)
                put_sym = bybit_symbol(symbol, expiration, "P", short_put["strike"])
                call_sym = bybit_symbol(symbol, expiration, "C", long_call["strike"])
                legs = [_leg(short_put, put_sym, "SELL_TO_OPEN"),
                        _leg(long_call, call_sym, "BUY_TO_OPEN")]
                # Downside is bounded by the short put strike (worst case: spot -> 0)
                max_loss = round(max(short_put["strike"] - net, 0.0), 2)
                desc = f"Long risk reversal (fade crash): sell {short_put['strike']}P / buy {long_call['strike']}C"
            else:
                short_call = nearest_by_delta(sides["calls"], target)
                long_put = nearest_by_delta(sides["puts"], -target)
                if not short_call or not long_put:
                    continue
                net = round(long_put["ask"] - short_call["bid"], 2)
                call_sym = bybit_symbol(symbol, expiration, "C", short_call["strike"])
                put_sym = bybit_symbol(symbol, expiration, "P", long_put["strike"])
                legs = [_leg(short_call, call_sym, "SELL_TO_OPEN"),
                        _leg(long_put, put_sym, "BUY_TO_OPEN")]
                # Upside on the short call is theoretically unbounded — leave
                # max_loss_per_contract unset and rely on risk_manager's
                # conservative debit/credit fallback plus portfolio caps.
                desc = f"Short risk reversal (fade euphoria): sell {short_call['strike']}C / buy {long_put['strike']}P"

            score = round(min(1.0, abs(strength) * 0.8 + 0.2), 3)

            candidate = {
                "symbol": symbol,
                "strategy": "risk_reversal",
                "description": desc,
                "legs_summary": " / ".join(l["occ_symbol"] for l in legs),
                "legs": legs,
                "expiration": expiration,
                "dte": days,
                "est_price": net,
                "is_credit": net < 0,
                "score": score,
                "rationale": (f"Trend strength {strength:+.2f} ({mode}), {days}DTE. "
                              f"Needs a tight stop-on-continuation exit if the move keeps "
                              f"going — the short leg here is the toxic side."),
            }
            if max_loss is not None:
                candidate["max_loss_per_contract"] = max_loss
            candidates.append(candidate)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
