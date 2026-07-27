# Crypto Options Bot (Bybit)

A rules-based scanner for crypto options on Bybit. Uses the same approval/learning/guardrails architecture as the original Schwab bot, adapted for crypto options.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `config.yaml` and fill in your Bybit API key/secret, Telegram bot token, etc.
3. Run the bot: `python scanner.py`

## Differences from the original

- **Symbol format**: Bybit options use `BTC-27DEC24-50000-C` style.
- **Contract multiplier**: 1 (not 100).
- **No early assignment** (European-style).
- **24/7 market**: no market hours restrictions.
- **Strategies**: credit spreads, directional, wheel (cash-secured puts, covered calls). Earnings_vol is disabled.
- **Sentiment** is disabled by default; you can add crypto-specific macro indicators later.

## Paper Trading

Set `mode: paper` in config.yaml. Paper trades are simulated using real-time quotes from Bybit's public API.

## Risk Management

The bot limits position size based on `max_risk_per_trade_pct` of account equity. It also respects `max_concurrent_positions` and `max_new_trades_per_day`.

## Notes

- This bot is for educational and personal use. Test thoroughly on testnet first.
- Bybit's testnet is recommended: set `testnet: true` in config.