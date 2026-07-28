"""
Strategy D: Event-Driven Straddles (Gamma Scalping).

Edge: ahead of major macro events (FOMC, CPI, ETF decisions, BTC
halvings) options get overbought and IV inflates; this module sells
ATM straddles 1-3 days out when IV rank is elevated, aiming to capture
the pre-event vol crush (closing before the print, per the strategy
brief, is an exit_manager/manual-approval decision, not something this
scanner does). In the day right after a listed event it flips to
BUYING ATM straddles if IV is still cheap, to scalp gamma on the
post-event move.

Events are configured (not fetched live) via
strategies.event_straddle.events in config.yaml — keep this list
updated with upcoming FOMC/CPI/ETF-decision dates, e.g.:

  strategies:
    event_straddle:
      events:
        - {name: "FOMC", date: "2026-09-16"}
        - {name: "CPI",  date: "2026-08-13"}
"""

import logging
from datetime import date

from strategies.base import dte, bybit_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.event_straddle")

DEFAULTS = {
    "enabled": True,
    "events": [],  # list of {"name": "FOMC", "date": "2026-08-12"} (ISO dates)
    "pre_event_days_before": 3,
    "pre_event_min_iv_rank": 80,
    "post_event_days_after": 1,
    "post_event_max_iv_rank": 60,
    "dte_min": 3,
    "dte_max": 21,
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


def _nearest_event(events: list[dict], today: date):
    best = None
    for e in events:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError, TypeError):
            continue
        days_until = (d - today).days
        if best is None or abs(days_until) < abs(best[1]):
            best = (e, days_until)
    return best  # (event_dict, days_until) or None


def _atm_straddle(symbol, expiration, sides, instruction):
    calls, puts = sides["calls"], sides["puts"]
    if not calls or not puts:
        return None
    call = nearest_by_delta(calls, 0.50)
    if not call:
        return None
    put = min(puts, key=lambda o: abs(o["strike"] - call["strike"]), default=None)
    if not put:
        return None
    call_sym = bybit_symbol(symbol, expiration, "C", call["strike"])
    put_sym = bybit_symbol(symbol, expiration, "P", put["strike"])
    return call, put, [_leg(call, call_sym, instruction), _leg(put, put_sym, instruction)]


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    s_cfg = {**DEFAULTS, **cfg.get("strategies", {}).get("event_straddle", {})}
    if not s_cfg["enabled"] or not s_cfg["events"]:
        return []
    candidates = []
    today = date.today()
    found = _nearest_event(s_cfg["events"], today)
    if not found:
        return []
    event, days_until = found

    is_pre_event = 0 < days_until <= s_cfg["pre_event_days_before"]
    is_post_event = -s_cfg["post_event_days_after"] <= days_until <= 0
    if not is_pre_event and not is_post_event:
        return []

    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)

        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            if days < max(days_until, 0):
                continue  # expiration must cover the event

            if is_pre_event and rank >= s_cfg["pre_event_min_iv_rank"]:
                built = _atm_straddle(symbol, expiration, sides, "SELL_TO_OPEN")
                if not built:
                    continue
                call, put, legs = built
                credit = round(call["bid"] + put["bid"], 2)
                candidates.append({
                    "symbol": symbol, "strategy": "event_straddle",
                    "description": f"Pre-{event.get('name', 'event')} vol-crush short straddle @ {call['strike']}",
                    "legs_summary": f"Sell {call['strike']}C / Sell {put['strike']}P",
                    "legs": legs, "expiration": expiration, "dte": days,
                    "est_price": credit, "is_credit": True,
                    "score": round(min(1.0, rank / 100), 3),
                    "rationale": (f"{event.get('name', 'Event')} in {days_until}d, IV rank {rank:.0f}. "
                                  f"Plan: close ~30min before the event to bank the crush, "
                                  f"before the binary print."),
                })

            elif is_post_event and rank <= s_cfg["post_event_max_iv_rank"]:
                built = _atm_straddle(symbol, expiration, sides, "BUY_TO_OPEN")
                if not built:
                    continue
                call, put, legs = built
                debit = round(call["ask"] + put["ask"], 2)
                candidates.append({
                    "symbol": symbol, "strategy": "event_straddle",
                    "description": f"Post-{event.get('name', 'event')} gamma-scalp long straddle @ {call['strike']}",
                    "legs_summary": f"Buy {call['strike']}C / Buy {put['strike']}P",
                    "legs": legs, "expiration": expiration, "dte": days,
                    "est_price": debit, "is_credit": False,
                    "max_loss_per_contract": debit,
                    "score": round(max(0.0, 1.0 - rank / 100), 3),
                    "rationale": (f"{event.get('name', 'Event')} was {abs(days_until)}d ago, IV rank "
                                  f"only {rank:.0f} — cheap gamma. Delta-hedge the perp every "
                                  f"1-2% move (hedge_manager.py handles the schedule)."),
                })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
