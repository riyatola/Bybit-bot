"""
Bybit client adapter: wraps Bybit's REST API with a Schwab-shaped
interface so executor.py / trade_tracker.py / scanner.py can call it
without being rewritten.

Schwab-shaped calls we implement:
  client.place_order(account_hash, payload)     -> resp{status_code, headers{Location}, text, json()}
  client.account_details(hash, fields="positions") -> resp{raise_for_status, json()["securitiesAccount"]}
  client.transactions(hash, symbol, types)      -> resp{raise_for_status, json()[transactions with netAmount]}
  client.account_info()                         -> info or raises

The real Bybit SDK (pybit) is used when installed. Everything degrades
gracefully if pybit isn't installed yet (raises on calls that would
touch the network, same as if auth failed).
"""

import json
import logging
import sys
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("bybit_client")

try:
    from pybit.exceptions import FailedRequestError
except ImportError:
    FailedRequestError = None  # type: ignore


def _format_api_error(exc: Exception, testnet: bool = True) -> str:
    """ASCII-safe error text with hints for common Bybit auth failures."""
    text = str(exc).replace("\u2192", "->").replace("\u2014", "-")
    if "401" in text or "ErrCode: 401" in text:
        env = "testnet" if testnet else "mainnet"
        portal = (
            "https://testnet.bybit.com/app/user/api-management"
            if testnet
            else "https://www.bybit.com/app/user/api-management"
        )
        text += (
            f" | Bybit {env} rejected these API credentials. Create keys at {portal}, "
            f"set testnet: {str(testnet).lower()} in config.yaml, and enable "
            "Read + Trade permissions (Unified account). Mainnet keys do not work on testnet."
        )
    return text

try:
    from pybit.unified_trading import HTTP
    _HAS_PYBIT = True
except ImportError:
    _HAS_PYBIT = False
    log.warning("pybit not installed — pip install pybit to enable live Bybit trading. "
                "Paper-only mode will still work via paper_broker.")


class _FakeResponse:
    """Mimics the subset of requests.Response used by executor.py / trade_tracker.py."""

    def __init__(self, status_code: int, body: dict | list | None = None,
                 headers: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers if headers is not None else {}
        self._text = text

    def json(self):
        return self._body

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return json.dumps(self._body)

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class BybitClient:
    """Thin adapter: accepts Schwab-shaped payloads, translates to Bybit REST calls."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self._session = None
        if _HAS_PYBIT:
            try:
                self._session = HTTP(
                    testnet=testnet,
                    api_key=api_key,
                    api_secret=api_secret,
                )
            except Exception as e:
                log.warning("Failed to initialize pybit HTTP session: %s", e)
                self._session = None

    # ---------------- connectivity / account ----------------

    def account_info(self) -> dict:
        """Light connectivity + wallet balance probe. Returns wallet summary."""
        if not self._session:
            if _HAS_PYBIT:
                raise RuntimeError("pybit session not initialized — check API key/secret.")
            raise RuntimeError("pybit not installed. Install with: pip install pybit")
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
            return resp
        except Exception as e:
            msg = _format_api_error(e, testnet=self.testnet)
            log.error("account_info failed: %s", msg)
            raise RuntimeError(msg) from e

    def account_details(self, account_hash: str, fields: str = "positions") -> _FakeResponse:
        """Schwab-compatible: returns json()['securitiesAccount']['positions']."""
        positions = []
        balances = {"cash_available": 0.0, "net_liq": 0.0}

        if self._session:
            try:
                wb = self._session.get_wallet_balance(accountType="UNIFIED")
                for coin in wb.get("result", {}).get("list", []):
                    usd = float(coin.get("totalAvailableBalance", 0) or 0)
                    balances["cash_available"] += usd
                    balances["net_liq"] += float(coin.get("totalEquity", 0) or 0)
            except Exception as e:
                log.warning("get_wallet_balance failed: %s", e)

            if fields and "positions" in fields.lower():
                try:
                    positions = self._fetch_derivative_positions() + self._fetch_option_positions()
                except Exception as e:
                    log.warning("fetching positions failed: %s", e)

        body = {
            "securitiesAccount": {
                "accountNumber": account_hash,
                "cash_available": balances["cash_available"],
                "net_liq": balances["net_liq"],
                "positions": positions,
            }
        }
        return _FakeResponse(200, body=body)

    def _fetch_derivative_positions(self) -> list[dict]:
        out = []
        try:
            resp = self._session.get_positions(category="linear", settleCoin="USDT")
            for p in resp.get("result", {}).get("list", []):
                qty = float(p.get("size", 0) or 0)
                if qty == 0:
                    continue
                symbol = p.get("symbol", "").removesuffix("USDT").removesuffix("PERP")
                out.append({
                    "instrument": {
                        "symbol": symbol,
                        "assetType": "CRYPTO",
                    },
                    "longQuantity": abs(qty),
                    "shortQuantity": 0 if qty > 0 else abs(qty),
                    "averagePrice": float(p.get("avgPrice", 0) or 0),
                    "marketValue": float(p.get("positionValue", 0) or 0),
                    "unrealizedProfit": float(p.get("unrealisedPnl", 0) or 0),
                })
        except Exception as e:
            log.debug("_fetch_derivative_positions: %s", e)
        return out

    def _fetch_option_positions(self) -> list[dict]:
        out = []
        try:
            resp = self._session.get_positions(category="option", settleCoin="USDC")
            for p in resp.get("result", {}).get("list", []):
                qty = float(p.get("size", 0) or 0)
                if qty == 0:
                    continue
                sym = p.get("symbol", "")
                out.append({
                    "instrument": {
                        "symbol": sym,
                        "assetType": "OPTION",
                    },
                    "longQuantity": abs(qty),
                    "shortQuantity": 0 if qty > 0 else abs(qty),
                    "averagePrice": float(p.get("avgPrice", 0) or 0),
                    "marketValue": float(p.get("positionValue", 0) or 0),
                    "unrealizedProfit": float(p.get("unrealisedPnl", 0) or 0),
                })
        except Exception as e:
            log.debug("_fetch_option_positions: %s", e)
        return out

    # ---------------- orders ----------------

    def place_order(self, account_hash: str, schwab_payload: dict) -> _FakeResponse:
        """
        Accepts a Schwab-shaped order payload:
          {orderType: NET_CREDIT|NET_DEBIT|LIMIT|MARKET,
           orderStrategyType: SINGLE,
           price: str,
           orderLegCollection: [{instruction, quantity, instrument{symbol, assetType}}]}

        Translates each leg to a Bybit order (category=option or linear). The
        PaperBroker bypasses this method entirely, so we only translate for
        real-money live trading.
        """
        legs = schwab_payload.get("orderLegCollection", [])
        if not legs:
            return _FakeResponse(400, body={"error": "empty orderLegCollection"})

        order_ids = []
        last_status = 201
        for leg in legs:
            instr = leg.get("instrument", {})
            symbol = instr.get("symbol", "")
            asset_type = instr.get("assetType", "OPTION").upper()
            qty = int(leg.get("quantity", 0))
            if qty <= 0:
                continue
            instruction = leg.get("instruction", "BUY_TO_OPEN")
            side = self._translate_side(instruction)

            if asset_type == "CRYPTO":
                category = "linear"
            elif asset_type == "OPTION":
                category = "option"
            else:
                category = "option"

            order_type = self._translate_order_type(schwab_payload.get("orderType", ""))
            price = None
            if order_type != "Market":
                price_str = schwab_payload.get("price")
                if price_str:
                    try:
                        price = f"{float(price_str):.4f}"
                    except ValueError:
                        price = None

            params = {
                "category": category,
                "symbol": symbol,
                "side": side,
                "orderType": order_type,
                "qty": str(qty),
            }
            if price:
                params["price"] = price
            if category == "option":
                params.setdefault("orderFilter", "Order")

            if not self._session:
                order_id = f"SIM-{uuid.uuid4().hex[:12]}"
                order_ids.append(order_id)
                continue

            try:
                resp = self._session.place_order(**params)
                oid = resp.get("result", {}).get("orderId", f"BYBIT-{uuid.uuid4().hex[:8]}")
                order_ids.append(oid)
            except Exception as e:
                log.error("place_order leg failed (%s): %s", symbol, e)
                last_status = 500
                break

        order_id = order_ids[0] if order_ids else f"UNKNOWN-{uuid.uuid4().hex[:6]}"
        body = {"order_id": order_id, "legs": order_ids}
        return _FakeResponse(
            status_code=last_status,
            body=body,
            headers={"Location": f"/bybit/orders/{order_id}"},
        )

    # ---------------- transactions (for trade_tracker outcome polling) ----------------

    def transactions(self, account_hash: str, symbol: str | None = None,
                     types: str = "TRADE", start_date: str | None = None,
                     end_date: str | None = None) -> _FakeResponse:
        """Schwab-compatible: returns a list of transactions with a netAmount field."""
        txns: list[dict] = []

        if self._session:
            end_ts = int(datetime.now().timestamp() * 1000)
            start_ts = int((datetime.now() - timedelta(days=120)).timestamp() * 1000)
            try:
                resp = self._session.get_transaction_log(
                    accountType="UNIFIED",
                    category="option",
                    startTime=start_ts,
                    endTime=end_ts,
                    limit=100,
                )
                for t in resp.get("result", {}).get("list", []):
                    amt = float(t.get("change", 0) or 0)
                    if amt == 0:
                        continue
                    txns.append({
                        "id": t.get("id"),
                        "symbol": t.get("symbol", symbol or ""),
                        "transactionSubType": t.get("type", types),
                        "netAmount": amt,
                        "date": t.get("transactionTime", ""),
                    })
            except Exception as e:
                log.debug("transactions lookup failed: %s", e)

        if symbol:
            txns = [t for t in txns if symbol.lower() in t.get("symbol", "").lower()]
        return _FakeResponse(200, body={"transactions": txns})

    # ---------------- option chain + quoting helpers (market_data delegates here) ----------------

    def get_option_chain_bybit(self, underlying: str, category: str = "option",
                               base_coin: str | None = None) -> list[dict]:
        """Raw Bybit option instruments. Returns list of instrument defs with strike/expiry/type."""
        if not self._session:
            return []
        base = base_coin or underlying
        try:
            resp = self._session.get_instruments_info(
                category=category,
                baseCoin=base,
                limit=1000,
            )
            return resp.get("result", {}).get("list", [])
        except Exception as e:
            log.debug("get_option_chain_bybit %s: %s", base, e)
            return []

    def get_option_tickers(self, base_coin: str = "BTC") -> list[dict]:
        """Get option tickers (live bid/ask/mark) for a base coin."""
        if not self._session:
            return []
        try:
            resp = self._session.get_tickers(category="option", baseCoin=base_coin)
            return resp.get("result", {}).get("list", [])
        except Exception as e:
            log.debug("get_option_tickers %s: %s", base_coin, e)
            return []

    def get_linear_tickers(self, category: str = "linear") -> list[dict]:
        if not self._session:
            return []
        try:
            resp = self._session.get_tickers(category=category)
            return resp.get("result", {}).get("list", [])
        except Exception as e:
            log.debug("get_linear_tickers: %s", e)
            return []

    def get_linear_ticker(self, symbol: str) -> dict | None:
        """Single linear/perp ticker (funding rate, OI, mark price). symbol e.g. BTCUSDT."""
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = f"{sym}USDT"
        try:
            if self._session:
                resp = self._session.get_tickers(category="linear", symbol=sym)
                items = resp.get("result", {}).get("list", [])
                return items[0] if items else None
            data = self._public_get("/v5/market/tickers", {"category": "linear", "symbol": sym})
            items = (data or {}).get("list", [])
            return items[0] if items else None
        except Exception as e:
            log.debug("get_linear_ticker %s: %s", sym, e)
            return None

    def get_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 3) -> list[dict]:
        """Account long/short holder ratios for a linear symbol."""
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = f"{sym}USDT"
        params = {"category": "linear", "symbol": sym, "period": period, "limit": limit}
        try:
            if self._session:
                resp = self._session.get_long_short_ratio(**params)
                if resp.get("retCode") == 0:
                    return resp.get("result", {}).get("list", [])
            data = self._public_get("/v5/market/account-ratio", params)
            return (data or {}).get("list", [])
        except Exception as e:
            log.debug("get_long_short_ratio %s: %s", sym, e)
            return []

    def _public_get(self, path: str, params: dict) -> dict | None:
        """Public market endpoint — works without pybit or API keys."""
        import requests
        host = "https://api-testnet.bybit.com" if self.testnet else "https://api.bybit.com"
        try:
            resp = requests.get(f"{host}{path}", params=params, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            if body.get("retCode") != 0:
                log.debug("Bybit public GET %s: %s", path, body.get("retMsg"))
                return None
            return body.get("result")
        except Exception as e:
            log.debug("public GET %s failed: %s", path, e)
            return None

    # ---------------- translation helpers ----------------

    @staticmethod
    def _translate_side(instruction: str) -> str:
        """Schwab instructions -> Bybit side.
        BUY_TO_OPEN / BUY_TO_CLOSE  -> Buy
        SELL_TO_OPEN / SELL_TO_CLOSE -> Sell"""
        upper = instruction.upper()
        if upper.startswith("BUY"):
            return "Buy"
        if upper.startswith("SELL"):
            return "Sell"
        return "Buy" if "BUY" in upper else "Sell"

    @staticmethod
    def _translate_order_type(schwab_order_type: str) -> str:
        """Schwab NET_CREDIT/NET_DEBIT/LIMIT -> Bybit Limit/Market."""
        ot = (schwab_order_type or "").upper()
        if ot in ("MARKET",):
            return "Market"
        return "Limit"
