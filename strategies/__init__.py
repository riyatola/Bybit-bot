"""
Strategies package. Each module inside this package exposes a scan()
function with the signature:

  credit_spreads.scan(client, symbols, cfg, market_data) -> list[candidate]
  directional.scan(client, symbols, cfg, market_data)    -> list[candidate]
  earnings_vol.scan(client, symbols, cfg, market_data)   -> list[candidate]
  wheel.scan(client, account_positions, cfg, market_data) -> list[candidate]

Base helpers live in strategies.base.
"""

__all__ = ["base", "credit_spreads", "directional", "earnings_vol", "wheel"]
