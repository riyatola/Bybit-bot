"""
Shared helper utilities for strategy modules.

Bybit option symbol convention:
  BTC-26AUG24-65000-C
  BTC-2026-08-28-65000-C   (alternative, used by synthetic feed)

We output the shorter form (26AUG24 expiry) as the primary Bybit
symbol, but parse_bybit_symbol handles both formats so parsing is
robust across live + synthetic feeds.
"""

import math
import re
from datetime import date, datetime, timedelta


def dte(expiration_str: str) -> int:
    """Days (calendar) between today and expiration_date string (ISO 2026-08-28)."""
    today = date.today()
    try:
        exp = date.fromisoformat(expiration_str)
    except ValueError:
        # Try alternative formats
        try:
            exp = datetime.strptime(expiration_str, "%y%b%d").date()
        except ValueError:
            try:
                exp = datetime.strptime(expiration_str, "%d%b%y").date()
            except ValueError:
                return 21
    return max(0, (exp - today).days)


def bybit_symbol(underlying: str, expiration: str, option_type_code: str, strike: float) -> str:
    """
    Builds a Bybit-format option symbol.

    :param underlying: "BTC", "ETH", etc.
    :param expiration: ISO date string "2026-08-28" (preferred), or "26AUG24".
    :param option_type_code: "C", "P" (or case-insensitive "call" / "put").
    :param strike: strike price (numeric). Stored as-is (no rounding here).
    :returns: e.g. "BTC-26AUG24-65000-C"
    """
    code = (option_type_code or "C").upper()
    if code in ("CALL", "C"):
        code = "C"
    elif code in ("PUT", "P"):
        code = "P"

    # Normalize expiry to short month-day-year format "26AUG24".
    exp_str = expiration
    if "-" in expiration and len(expiration) == 10:
        try:
            d_ = date.fromisoformat(expiration)
            exp_str = d_.strftime("%d%b%y").upper()
        except ValueError:
            pass
    # Strip any leading zeros from day (Bybit uses "5", not "05" for single-digit days)
    m = re.match(r"^0?(\d{1,2})([A-Z]{3})(\d{2})$", exp_str)
    if m:
        exp_str = f"{m.group(1)}{m.group(2)}{m.group(3)}"

    # Strike: integer if it's a whole number, else preserve decimals.
    if isinstance(strike, (int, float)) and float(strike).is_integer():
        strike_str = f"{int(strike)}"
    else:
        strike_str = f"{float(strike):g}"

    return f"{underlying.upper()}-{exp_str}-{strike_str}-{code}"


def parse_bybit_symbol(symbol: str) -> dict:
    """
    Parses a Bybit option symbol into components:
      underlying, expiration (ISO date), strike (float), option_type ('call'|'put'),
      bybit_symbol (original).
    Raises ValueError if unparseable.
    """
    if not symbol:
        raise ValueError("empty symbol")
    s = symbol.strip().upper()

    # Format 1:  BTC-26AUG24-65000-C
    # Format 2:  BTC-2026-08-28-65000-C
    m1 = re.match(r"^([A-Z0-9]+)-([0-9]{1,2}[A-Z]{3}[0-9]{2})-([0-9]+(?:\.[0-9]+)?)-([CP])$", s)
    m2 = re.match(r"^([A-Z0-9]+)-([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9]+(?:\.[0-9]+)?)-([CP])$", s)
    m3 = re.match(r"^([A-Z0-9]+)_([0-9]{1,2}[A-Z]{3}[0-9]{2})_([0-9]+(?:\.[0-9]+)?)-([CP])$", s)

    match = m1 or m2 or m3
    if not match:
        # Try splitting on dashes, more lenient (handles synthetic -strike-C tail)
        parts = s.split("-")
        if len(parts) >= 4:
            underlying = parts[0]
            strike = float(parts[-2])
            cp = parts[-1]
            exp_candidate = "-".join(parts[1:-2])
            try:
                d_ = date.fromisoformat(exp_candidate)
                exp_iso = d_.isoformat()
            except ValueError:
                try:
                    d_ = datetime.strptime(exp_candidate, "%y%b%d").date()
                    exp_iso = d_.isoformat()
                except ValueError:
                    try:
                        d_ = datetime.strptime(exp_candidate, "%d%b%y").date()
                        exp_iso = d_.isoformat()
                    except ValueError:
                        # Give up: default to 21 days out
                        d_ = date.today() + timedelta(days=21)
                        exp_iso = d_.isoformat()
            return {
                "bybit_symbol": symbol,
                "underlying": underlying,
                "expiration": exp_iso,
                "strike": strike,
                "option_type": "call" if cp == "C" else "put",
            }
        raise ValueError(f"unrecognized option symbol format: {symbol}")

    underlying = match.group(1)
    exp_raw = match.group(2)
    strike = float(match.group(3))
    cp = match.group(4)

    if re.match(r"^[0-9]{4}-", exp_raw):
        exp_iso = exp_raw
    else:
        # 26AUG24, 5AUG24 etc.
        # Handle 1- or 2-digit day prefix
        mday = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})$", exp_raw)
        if not mday:
            # Fallback: try DDMMMYY via strptime (two-digit day)
            try:
                d_ = datetime.strptime(exp_raw, "%d%b%y").date()
            except ValueError:
                # Prepend zero to day and try again
                try:
                    d_ = datetime.strptime("0" + exp_raw, "%d%b%y").date()
                except ValueError:
                    d_ = date.today() + timedelta(days=21)
            exp_iso = d_.isoformat()
        else:
            day_nz = mday.group(1).zfill(2)
            mon = mday.group(2)
            yr = mday.group(3)
            d_ = datetime.strptime(f"{day_nz}{mon}{yr}", "%d%b%y").date()
            exp_iso = d_.isoformat()

    return {
        "bybit_symbol": symbol,
        "underlying": underlying,
        "expiration": exp_iso,
        "strike": strike,
        "option_type": "call" if cp == "C" else "put",
    }


def nearest_by_delta(options: list[dict], target_delta: float) -> dict | None:
    """Given a list of options (dicts with a 'delta' key), return the one
    whose delta is closest to target_delta (signed)."""
    if not options:
        return None
    tgt = float(target_delta)
    best = None
    best_dist = math.inf
    for o in options:
        d = o.get("delta")
        if d is None:
            continue
        dist = abs(float(d) - tgt)
        if dist < best_dist:
            best_dist = dist
            best = o
    return best


def filter_liquid_chain(sides: dict, max_spread_pct: float) -> dict:
    """Given an expiration's {"calls": [...], "puts": [...]} dict, drop any
    option whose bid/ask spread (as percent of mid) exceeds
    max_spread_pct, or whose bid/ask are non-positive. Returns a new dict
    of the same shape, not mutated input."""
    out: dict = {"calls": [], "puts": []}
    for side in ("calls", "puts"):
        for o in sides.get(side, []):
            bid = float(o.get("bid", 0) or 0)
            ask = float(o.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2.0
            if mid <= 0:
                continue
            spread_pct = (ask - bid) / mid
            if spread_pct > float(max_spread_pct):
                continue
            out[side].append(o)
    return out
