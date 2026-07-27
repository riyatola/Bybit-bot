"""
Earnings volatility scanner (crypto placeholder).

For equities this scans for upcoming earnings and constructs straddles.
Crypto doesn't have earnings — this module returns an empty list by
default and is provided so scanner.py's unconditional import succeeds.
If you want to adapt it for macro events (FOMC, ETF decisions, BTC
halvings, etc.), populate MACRO_EVENT_CALENDAR below.
"""

import logging

log = logging.getLogger("strategies.earnings_vol")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    """Returns empty list by default. Crypto has no earnings calendar."""
    return []
