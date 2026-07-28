"""
Strategy A: Systematic VRP Harvesting — short strangles, delta-hedged with perps.

Edge: crypto options routinely trade at an implied-vol premium over
realized vol. This module sells far-OTM (10-15 delta) calls and puts,
7-14 DTE, and returns the two option legs as one approval-worthy
candidate. Delta hedging itself (shorting/buying the perp to flatten
net delta, and re-hedging every N hours) is NOT done inline in this
scanner — see hedge_manager.py, which runs on its own schedule against
*all* open option positions (this strategy's included) and keeps the
book delta-flat with perp orders. That split keeps this module a pure
"what to sell" scanner, consistent with credit_spreads.py / wheel.py.

Risk control baked in here:
  - Refuses to sell when iv_rank > max_iv_rank_to_sell (vol can always
    go higher in crypto — this is the single most important guardrail
    for this strategy).
  - Refuses to sell when iv_rank < min_iv_rank (not rich enough to harvest).
  - Reports per-leg vega/delta/gamma/theta on each leg so risk_manager's
    portfolio-greeks gate (max_vega_usd) can actually see and cap this
    strategy's book, since naked strangles are the strategy most likely
    to blow through a vega cap.
"""

import logging
from strategies.base import dte, bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.vrp_strangle")

DEFAULTS = {
    "enabled": True,
    "min_iv_rank": 40,
    "max_iv_rank_to_sell": 85,     # HARD STOP: never sell strangles above this IV rank
    "target_short_delta": 0.125,   # midpoint of the 10-15 delta target band
    "dte_min": 7,
    "dte_max": 14,
    "max_leg_spread_pct": 0.20,
    "min_credit_dollars": 5.0,
}


def _leg(option: dict, occ_symbol: str) -> dict:
    return {
        "occ_symbol": occ_symbol,
        "instruction": "SELL_TO_OPEN",
        "ratio": 1,
        "quantity": 1,
        "delta": option.get("delta", 0.0),
        "gamma": option.get("gamma", 0.0),
        "vega": option.get("vega", 0.0),
        "theta": option.get("theta", 0.0),
    }


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("vrp_strangle", {})}
    if not s_cfg["enabled"]:
        return []
    candidates = []

    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        if rank > s_cfg["max_iv_rank_to_sell"]:
            log.info("%s: iv_rank %.0f > %.0f cap — skipping strangle sale (vol regime too hot)",
                     symbol, rank, s_cfg["max_iv_rank_to_sell"])
            continue
        if rank < s_cfg["min_iv_rank"]:
            continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)

        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)

            short_call = nearest_by_delta(sides["calls"], s_cfg["target_short_delta"])
            short_put = nearest_by_delta(sides["puts"], -s_cfg["target_short_delta"])
            if not short_call or not short_put:
                continue

            credit = round(short_call["bid"] + short_put["bid"], 2)
            if credit < s_cfg["min_credit_dollars"]:
                continue

            call_sym = bybit_symbol(symbol, expiration, "C", short_call["strike"])
            put_sym = bybit_symbol(symbol, expiration, "P", short_put["strike"])

            # Naked strangle: max loss is theoretically unbounded, so we
            # deliberately leave max_loss_per_contract unset and let
            # risk_manager's fallback (2x credit) size it conservatively,
            # then lean on the portfolio vega/delta caps (see docstring)
            # as the real backstop rather than a fabricated number here.
            delta_band_fit = 1.0 - min(
                abs(abs(short_call["delta"]) - s_cfg["target_short_delta"]),
                abs(abs(short_put["delta"]) - s_cfg["target_short_delta"]),
            ) * 4.0
            score = max(0.0, min(1.0, (rank / 100) * 0.65 + max(delta_band_fit, 0) * 0.35))

            candidates.append({
                "symbol": symbol,
                "strategy": "vrp_strangle",
                "description": (f"Short strangle (VRP harvest), {days}DTE, "
                                 f"~{short_call['delta']:.2f}/{short_put['delta']:.2f} delta"),
                "legs_summary": f"Sell {short_call['strike']}C / Sell {short_put['strike']}P",
                "legs": [
                    _leg(short_call, call_sym),
                    _leg(short_put, put_sym),
                ],
                "expiration": expiration,
                "dte": days,
                "est_price": credit,
                "is_credit": True,
                "score": round(score, 3),
                "rationale": (f"IV rank {rank:.0f} (cap {s_cfg['max_iv_rank_to_sell']:.0f}), "
                              f"credit ${credit:.2f}, {days}DTE. Needs continuous delta "
                              f"hedging via hedge_manager.py — this candidate is unhedged "
                              f"until the next hedge cycle runs."),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
