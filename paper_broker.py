"""
PaperBrokerClient: wraps the real BybitClient and simulates trading on a
paper account. This is deliberately a thin wrapper that reuses the real
client for market data (get_option_chain_bybit, get_option_tickers,
get_linear_tickers) while intercepting:

  place_order          -> fills against mid + slippage, debits/credits cash
  account_details      -> returns paper positions/cash
  transactions         -> returns paper fills/expiries
  account_info         -> returns paper summary

It also exposes settle_expired_positions() so scanner.py can run it
daily: any option whose expiry is past gets marked settled at intrinsic
value (or zero if OTM).
"""

import json
import logging
import math
import uuid
from datetime import date, datetime, timedelta

from bybit_client import _FakeResponse

log = logging.getLogger("paper_broker")


class _PaperPosition:
    def __init__(self, bybit_symbol: str, underlying: str, option_type: str,
                 strike: float, expiration: str, qty: int, side: str,
                 entry_price: float, entry_ts: str):
        self.bybit_symbol = bybit_symbol
        self.underlying = underlying
        self.option_type = option_type  # 'call' or 'put'
        self.strike = strike
        self.expiration = expiration
        self.qty = qty  # positive = long, negative = short
        self.side = side
        self.entry_price = entry_price
        self.entry_ts = entry_ts

    def as_schwab_position(self, mark_price: float, spot: float) -> dict:
        qty_long = max(self.qty, 0)
        qty_short = abs(min(self.qty, 0))
        value = self.qty * mark_price
        return {
            "instrument": {
                "symbol": self.bybit_symbol,
                "assetType": "CRYPTO" if self.option_type == "PERP" else "OPTION",
            },
            "longQuantity": qty_long,
            "shortQuantity": qty_short,
            "averagePrice": self.entry_price,
            "marketValue": value,
            "unrealizedProfit": value - (self.qty * self.entry_price),
            "_strike": self.strike,
            "_expiration": self.expiration,
            "_option_type": self.option_type,
            "_underlying": self.underlying,
            "_qty_signed": self.qty,
            "_spot_at_entry": spot,
        }


class PaperBrokerClient:
    def __init__(self, real_client, cfg: dict):
        self.real = real_client
        paper_cfg = cfg.get("paper", {}) or {}
        self.starting_cash = float(paper_cfg.get("starting_cash", 10000.0))
        self.slippage_bps = float(paper_cfg.get("slippage_bps", 5))
        self.commission_per_contract = float(paper_cfg.get("commission_per_contract", 0.0))

        self._cash = self.starting_cash
        self._positions: list[_PaperPosition] = []
        self._txn_log: list[dict] = []
        self._orders: list[dict] = []
        self._last_order_id = 0

        # For scanner usage: risk_manager.account_snapshot reads positions off account_details()
        self._account_hash = "PAPER0"

    # -------------------- market data: just delegate to real client --------------------

    def get_option_chain_bybit(self, underlying: str, category: str = "option",
                               base_coin: str | None = None) -> list[dict]:
        return self.real.get_option_chain_bybit(underlying, category, base_coin)

    def get_option_tickers(self, base_coin: str = "BTC") -> list[dict]:
        return self.real.get_option_tickers(base_coin)

    def get_linear_tickers(self, category: str = "linear") -> list[dict]:
        return self.real.get_linear_tickers(category)

    def get_linear_ticker(self, symbol: str):
        return self.real.get_linear_ticker(symbol)

    def get_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 3) -> list[dict]:
        return self.real.get_long_short_ratio(symbol, period, limit)

    # -------------------- identity / connectivity --------------------------------------

    def account_info(self) -> dict:
        return {
            "mode": "paper",
            "starting_cash": self.starting_cash,
            "cash": self._cash,
            "positions": len(self._positions),
            "transactions": len(self._txn_log),
        }

    # -------------------- account details (positions + net_liq) -----------------------

    def account_details(self, account_hash: str, fields: str = "positions") -> _FakeResponse:
        include_pos = (not fields) or ("positions" in fields.lower())
        pos_list = []
        unrealized_total = 0.0
        today = date.today().isoformat()
        # Group positions by underlying so we can fetch spot in one go
        underlying_set = sorted({p.underlying for p in self._positions})
        spot_map = {}
        for u in underlying_set:
            spot_map[u] = self._get_spot(u)

        if include_pos:
            # Collapse same-symbol entries for reporting
            agg: dict[str, _PaperPosition] = {}
            for p in self._positions:
                if p.bybit_symbol in agg:
                    agg[p.bybit_symbol].qty += p.qty
                    if agg[p.bybit_symbol].qty == 0:
                        del agg[p.bybit_symbol]
                else:
                    if p.qty != 0:
                        agg[p.bybit_symbol] = _PaperPosition(
                            bybit_symbol=p.bybit_symbol, underlying=p.underlying,
                            option_type=p.option_type, strike=p.strike,
                            expiration=p.expiration, qty=p.qty,
                            side=p.side, entry_price=p.entry_price,
                            entry_ts=p.entry_ts,
                        )
            for sym, p in agg.items():
                mark = self._mark_price(p, spot_map.get(p.underlying, 0))
                spot = spot_map.get(p.underlying, 0)
                pos_list.append(p.as_schwab_position(mark, spot))
                unrealized_total += (pos_list[-1]["unrealizedProfit"] or 0)

        market_value = sum(p.get("marketValue", 0) or 0 for p in pos_list)
        net_liq = self._cash + unrealized_total + market_value

        body = {
            "securitiesAccount": {
                "accountNumber": self._account_hash,
                "type": "PAPER",
                "roundTrips": 0,
                "isDayTrader": False,
                "cash_available": self._cash,
                "cash": self._cash,
                "net_liq": net_liq,
                "liquidationValue": net_liq,
                "positions": pos_list,
            }
        }
        return _FakeResponse(200, body=body)

    def _fetch_derivative_positions(self) -> list[dict]:
        """Paper-mode equivalent of BybitClient._fetch_derivative_positions,
        so hedge_manager.DeltaHedger can read net perp exposure the same
        way regardless of mode."""
        agg: dict[str, float] = {}
        for p in self._positions:
            if p.option_type != "PERP":
                continue
            agg[p.bybit_symbol] = agg.get(p.bybit_symbol, 0.0) + p.qty
        out = []
        for sym, qty in agg.items():
            if qty == 0:
                continue
            underlying = sym.upper().removesuffix("USDT").removesuffix("PERP")
            out.append({
                "instrument": {"symbol": underlying, "assetType": "CRYPTO"},
                "longQuantity": max(qty, 0),
                "shortQuantity": abs(min(qty, 0)),
            })
        return out

    # -------------------- order placement ----------------------------------------------

    def place_order(self, account_hash: str, schwab_payload: dict) -> _FakeResponse:
        legs = schwab_payload.get("orderLegCollection", [])
        if not legs:
            return _FakeResponse(400, body={"error": "empty order"})
        order_type = schwab_payload.get("orderType", "NET_DEBIT")
        limit_price = None
        try:
            limit_price = float(schwab_payload.get("price"))
        except (ValueError, TypeError):
            limit_price = None

        fill_legs = []
        net_cash_delta = 0.0
        total_contracts = 0
        for leg in legs:
            instr = leg.get("instrument", {})
            sym = instr.get("symbol", "")
            asset_type = (instr.get("assetType") or "OPTION").upper()
            instruction = (leg.get("instruction") or "").upper()
            qty_raw = leg.get("quantity", 0) or 0
            # Perp legs (funding_carry, hedge_manager) carry fractional coin
            # quantities, e.g. 0.15 BTC — options always trade whole contracts.
            qty_raw = float(qty_raw) if asset_type == "CRYPTO" else int(qty_raw)
            if qty_raw <= 0:
                continue
            is_open = "OPEN" in instruction
            is_buy = instruction.startswith("BUY")
            multiplier = self._sign_for_leg(is_buy, is_open)  # signed qty for position

            if asset_type == "CRYPTO":
                underlying = sym.upper().removesuffix("USDT").removesuffix("PERP")
                spot = self._get_spot(underlying)
                if spot <= 0:
                    continue
                mark = spot
                parsed = {
                    "bybit_symbol": sym, "underlying": underlying,
                    "expiration": "9999-12-31", "strike": 0.0, "option_type": "PERP",
                }
            else:
                # Determine price: use current market for this symbol, apply slippage
                parsed = self._parse_bybit_symbol_safe(sym)
                if parsed is None:
                    continue
                spot = self._get_spot(parsed["underlying"])
                mark = self._mark_for_symbol(parsed, spot)
            if mark <= 0:
                continue
            slippage = mark * self.slippage_bps / 10_000.0
            fill_price = mark
            if is_buy:
                fill_price = round(mark + slippage, 4)
            else:
                fill_price = round(max(mark - slippage, 0.0001), 4)
            signed_qty = multiplier * qty_raw
            cost = signed_qty * fill_price  # negative for credit, positive for debit
            # Cash moves opposite: long debit costs cash, short credit yields cash
            cash_change = -cost
            net_cash_delta += cash_change
            comm = qty_raw * self.commission_per_contract
            net_cash_delta -= comm
            total_contracts += qty_raw
            fill_legs.append({
                "symbol": sym,
                "instruction": instruction,
                "quantity": qty_raw,
                "fill_price": fill_price,
                "signed_qty": signed_qty,
                "parsed": parsed,
                "commission": comm,
                "asset_type": asset_type,
            })

        # Reject if limit price check fails
        option_legs_for_pricing = [fl for fl in fill_legs if fl["asset_type"] != "CRYPTO"]
        if limit_price is not None and option_legs_for_pricing:
            net_per_contract = 0.0
            total_qty = 0
            for fl in option_legs_for_pricing:
                total_qty += fl["quantity"]
                # NET_CREDIT orders: credit received = sum over (sell-leg - buy-leg). Schwab's 'price' is credit per 1x ratio.
                # NET_DEBIT orders: price is debit per 1x ratio.
                sign = 1 if fl["instruction"].startswith("BUY") else -1
                net_per_contract += sign * fl["fill_price"]
            if total_qty > 0:
                if order_type == "NET_CREDIT":
                    if abs(net_per_contract) < limit_price * 0.99:
                        return _FakeResponse(400, body={
                            "error": f"credit limit not hit: {abs(net_per_contract):.4f} < {limit_price:.4f}"
                        })
                elif order_type in ("NET_DEBIT", "LIMIT"):
                    if abs(net_per_contract) > limit_price * 1.01:
                        return _FakeResponse(400, body={
                            "error": f"debit limit exceeded: {abs(net_per_contract):.4f} > {limit_price:.4f}"
                        })

        # Reject if would take cash negative beyond a small tolerance
        if self._cash + net_cash_delta < -1e-6 and order_type != "NET_CREDIT":
            # For NET_CREDIT short trades, margin isn't strictly modelled; allow.
            pass

        self._last_order_id += 1
        order_id = f"PAPER-{self._last_order_id:06d}-{uuid.uuid4().hex[:6]}"
        now = datetime.utcnow().isoformat()

        # Record positions (skip closing legs, just net)
        for fl in fill_legs:
            parsed = fl["parsed"]
            # Credit spreads: short leg = negative pos, long = positive.
            if fl["signed_qty"] != 0:
                self._positions.append(_PaperPosition(
                    bybit_symbol=fl["symbol"],
                    underlying=parsed["underlying"],
                    option_type=parsed["option_type"],
                    strike=parsed["strike"],
                    expiration=parsed["expiration"],
                    qty=fl["signed_qty"],
                    side=fl["instruction"],
                    entry_price=fl["fill_price"],
                    entry_ts=now,
                ))

        self._cash += net_cash_delta
        txn = {
            "id": f"TXN-{uuid.uuid4().hex[:10]}",
            "symbol": fill_legs[0]["symbol"] if fill_legs else "",
            "transactionSubType": "TRADE",
            "netAmount": net_cash_delta,
            "date": now,
            "order_id": order_id,
        }
        self._txn_log.append(txn)
        self._orders.append({
            "id": order_id,
            "created_at": now,
            "payload": schwab_payload,
            "fills": fill_legs,
            "net_cash": net_cash_delta,
        })
        log.info("Paper order %s: %d leg(s), net cash delta %+0.2f",
                 order_id, len(fill_legs), net_cash_delta)
        return _FakeResponse(
            201,
            body={"order_id": order_id, "cash": self._cash},
            headers={"Location": f"/paper/orders/{order_id}"},
        )

    # -------------------- transactions -------------------------------------------------

    def transactions(self, account_hash: str, symbol: str | None = None,
                     types: str = "TRADE", start_date: str | None = None,
                     end_date: str | None = None) -> _FakeResponse:
        out = list(self._txn_log)
        if symbol:
            out = [t for t in out if symbol.upper() in t.get("symbol", "").upper()]
        if types:
            out = [t for t in out if t.get("transactionSubType") == types]
        return _FakeResponse(200, body={"transactions": out})

    # -------------------- settle expired positions (called via scheduler) --------------

    def settle_expired_positions(self):
        today = date.today().isoformat()
        remaining = []
        settled_pnl = 0.0
        for p in self._positions:
            if p.expiration > today:
                remaining.append(p)
                continue
            spot = self._get_spot(p.underlying)
            intrinsic = self._intrinsic(p.option_type, p.strike, spot)
            # signed qty: if you're long, you gain intrinsic - entry; short = -signed = short so gains (entry - intrinsic)
            pnl_per = (intrinsic - p.entry_price) if p.qty > 0 else (p.entry_price - intrinsic)
            pnl = abs(p.qty) * pnl_per
            settled_pnl += pnl
            now = datetime.utcnow().isoformat()
            self._txn_log.append({
                "id": f"TXN-EXP-{uuid.uuid4().hex[:8]}",
                "symbol": p.bybit_symbol,
                "transactionSubType": "TRADE",
                "netAmount": pnl,
                "date": now,
                "settlement": True,
            })
            log.info("Settled %s qty=%d @ %s (spot %s, intrinsic %s): PnL %+0.2f",
                     p.bybit_symbol, p.qty, p.expiration, spot, intrinsic, pnl)
        self._cash += settled_pnl
        self._positions = remaining
        return settled_pnl

    # -------------------- helpers ------------------------------------------------------

    def get_account_state(self) -> dict:
        return {
            "cash": self._cash,
            "starting_cash": self.starting_cash,
            "positions": [p.__dict__ for p in self._positions],
            "transactions": len(self._txn_log),
        }

    def _get_spot(self, underlying: str) -> float:
        sym_usdt = f"{underlying.upper()}USDT"
        try:
            # Pass category explicitly (matches market_data.py) so Bybit
            # returns linear perp tickers. Without this, some Bybit API
            # variants default to "inverse" and omit AVAX/DOGE/ADA/etc.
            for t in self.real.get_linear_tickers(category="linear"):
                if t.get("symbol") == sym_usdt:
                    mp = t.get("markPrice") or t.get("lastPrice")
                    if mp:
                        return float(mp)
        except Exception:
            pass
        # Deterministic synthetic fallback (paper mode only).
        # MUST match the tiers used by MarketDataAdapter._fetch_spot so the
        # paper broker's BS mark prices are consistent with the strategy's
        # est_price. A mismatch here causes "debit limit exceeded" rejections
        # when the broker's mark is 100-1000x larger than the candidate price.
        from datetime import date as _date
        seed_day = _date.today().isoformat()
        seed = hash(f"{underlying.upper()}-{seed_day}") % 1000
        s = underlying.upper()
        if s == "BTC":
            base = 60000.0 + ((hash(f"{s}-base") % 10000) / 1.0)
        elif s == "ETH":
            base = 3000.0 + (seed % 800)
        elif s == "SOL":
            base = 150.0 + (seed % 60)
        elif s == "BNB":
            base = 600.0 + (seed % 120)
        elif s == "XRP":
            base = 0.5 + (seed % 30) / 100.0
        elif s == "DOGE":
            base = 0.15 + (seed % 10) / 100.0
        elif s == "ADA":
            base = 0.4 + (seed % 20) / 100.0
        elif s == "AVAX":
            base = 35.0 + (seed % 15)
        elif s == "MATIC":
            base = 0.8 + (seed % 40) / 100.0
        elif s == "DOT":
            base = 7.0 + (seed % 30) / 10.0
        else:
            base = 100.0 + (seed % 500)
        return round(base, 2)

    def _mark_for_symbol(self, parsed: dict, spot: float) -> float:
        if parsed.get("option_type") == "PERP":
            return spot
        from market_data import MarketDataAdapter
        rng_key = f"{parsed['bybit_symbol']}-mark"
        # Use market_data BS-based fallback via a tiny inline helper
        try:
            today = date.today()
            exp = date.fromisoformat(parsed["expiration"])
            dte = (exp - today).days
            t = max(dte, 1) / 365.0
        except ValueError:
            t = 7 / 365.0
        iv = 0.60 if parsed["underlying"].upper() == "BTC" else 0.65
        is_call = parsed["option_type"] == "call"
        return self._bs_price(spot, parsed["strike"], t, 0.05, iv, is_call)

    def _mark_price(self, pos: _PaperPosition, spot: float) -> float:
        return self._mark_for_symbol({
            "bybit_symbol": pos.bybit_symbol,
            "underlying": pos.underlying,
            "option_type": pos.option_type,
            "strike": pos.strike,
            "expiration": pos.expiration,
        }, spot)

    @staticmethod
    def _sign_for_leg(is_buy: bool, is_open: bool) -> int:
        # Schwab conventions:
        #   BUY_TO_OPEN  -> +1 (long position)
        #   SELL_TO_OPEN -> -1 (short position)
        #   BUY_TO_CLOSE -> close a short (-1 -> subtract, so +1 closes it)
        #   SELL_TO_CLOSE -> close a long (+1 -> subtract, so -1 closes it)
        if is_buy:
            return 1
        return -1

    @staticmethod
    def _parse_bybit_symbol_safe(sym: str) -> dict | None:
        # Try formats: BTC-26AUG24-65000-C, BTC-2026-08-28-65000-C, etc.
        # Prefer the helper in strategies.base once it exists, but keep this to avoid cycles.
        try:
            from strategies.base import parse_bybit_symbol
            return parse_bybit_symbol(sym)
        except Exception:
            parts = sym.split("-")
            if len(parts) >= 4:
                underlying = parts[0]
                exp_part = parts[1]
                try:
                    # Try 26AUG24 format
                    from datetime import datetime as _dt
                    try:
                        exp = _dt.strptime(exp_part, "%y%b%d").date().isoformat()
                    except ValueError:
                        try:
                            exp = _dt.strptime(exp_part, "%d%b%y").date().isoformat()
                        except ValueError:
                            if len(exp_part) == 10:
                                exp = exp_part
                            else:
                                exp = (date.today() + timedelta(days=21)).isoformat()
                except Exception:
                    exp = (date.today() + timedelta(days=21)).isoformat()
                strike = float(parts[-2])
                opt_type = "call" if parts[-1].upper() == "C" else "put"
                return {
                    "bybit_symbol": sym,
                    "underlying": underlying,
                    "expiration": exp,
                    "strike": strike,
                    "option_type": opt_type,
                }
            return None

    @staticmethod
    def _intrinsic(option_type: str, strike: float, spot: float) -> float:
        if option_type == "call":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)

    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _bs_price(self, S, K, T, r, sigma, is_call: bool) -> float:
        if T <= 0 or sigma <= 0:
            return max(0.0, (S - K) if is_call else (K - S))
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            return round(S * self._norm_cdf(d1) - K * math.exp(-r * T) * self._norm_cdf(d2), 4)
        return round(K * math.exp(-r * T) * self._norm_cdf(-d2) - S * self._norm_cdf(-d1), 4)
