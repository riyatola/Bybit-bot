"""
MarketDataAdapter: wraps the Bybit client (or a fall-back synthetic feed)
to present the interface strategies + risk + exit_manager + sentiment need.

Contract for the option chain (get_option_chain return shape):
  {
    "2026-08-28": {
        "calls": [{
            "symbol": bybit_symbol, "strike": 65000.0,
            "bid": 125.5, "ask": 130.0, "mark": 127.75,
            "delta": 0.42, "gamma": 0.001, "vega": 12.5, "theta": -1.1,
            "volume": 1234, "openInterest": 5678,
        }, ...],
        "puts": [{...same shape...}],
    }, ...
}

When the underlying Bybit session is not available (pybit missing or
testnet down), this adapter falls back to a deterministic synthetic
feed based on the last spot price plus a random seed per day, so scans
still produce candidates and the rest of the bot is exercisable.
"""

import logging
import math
import random
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("market_data")

_IV_HISTORY_CACHE = {}  # symbol -> list of (date, iv) tuples


class MarketDataAdapter:
    def __init__(self, client, cfg: dict | None = None):
        self.client = client
        self.cfg = cfg or {}
        self._cache = {}  # key -> (expiry_ts, payload)
        self._cache_ttl_sec = self.cfg.get("market_data_cache_ttl_sec", 300)
        self._spot_cache = {}
        self._seed_day = date.today().isoformat()

    # ---------- cached lookup ----------

    def _cached(self, key: str, factory, ttl_sec: int | None = None):
        now = time.time()
        ttl = ttl_sec or self._cache_ttl_sec
        entry = self._cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]
        val = factory()
        self._cache[key] = (now, val)
        return val

    # ---------- spot price + trend ----------

    def get_last_price(self, symbol: str) -> float | None:
        """Spot (index) price for a symbol like BTC, ETH, or $VIX.X."""
        if symbol.startswith("$"):
            # Synthetic macro indices — these aren't on Bybit; return a plausible value.
            if symbol.upper() in ("$VIX.X", "$VIX"):
                return self._synthetic_vix()
            return None

        spot = self._cached(f"spot:{symbol}", lambda: self._fetch_spot(symbol))
        return spot

    def _fetch_spot(self, symbol: str) -> float:
        # Try Bybit linear tickers first (BTCUSDT markPrice)
        sym_usdt = f"{symbol.upper()}USDT"
        try:
            tickers = self.client.get_linear_tickers(category="linear")
            for t in tickers:
                if t.get("symbol") == sym_usdt:
                    mp = t.get("markPrice") or t.get("lastPrice")
                    if mp:
                        return float(mp)
        except Exception as e:
            log.debug("_fetch_spot failed for %s: %s", symbol, e)

        seed = hash(f"{symbol}-{self._seed_day}") % 1000
        s = symbol.upper()
        if s == "BTC":
            base = 60000.0 + (seed % 10000)
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

    def _synthetic_vix(self) -> float:
        seed = hash(self._seed_day) % 100
        return round(15.0 + (seed % 15) / 10, 2)

    def get_trend(self, symbol: str) -> str:
        ts = self.get_trend_strength(symbol, 20)
        if ts > 0.15:
            return "up"
        if ts < -0.15:
            return "down"
        return "neutral"

    def get_trend_strength(self, symbol: str, lookback_days: int = 20) -> float:
        """Simple linear-regression slope over synthetic daily closes,
        normalized to (-1, +1). Positive = up."""
        bars = self._synthetic_bars(symbol, n=lookback_days)
        if len(bars) < 3:
            return 0.0
        xs = list(range(len(bars)))
        ys = [b["close"] for b in bars]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n)) or 1.0
        slope = num / den
        # Normalize by avg price and bar count so results are bounded.
        strength = slope / max(my, 1e-9) * min(n, 60) * 20.0
        return max(-1.0, min(1.0, strength))

    # ---------- volatility / volume ----------

    def get_iv_rank(self, symbol: str) -> float:
        """Implied volatility percentile 0..100 using 1-year synthetic history."""
        key = f"ivhist:{symbol}"
        if key not in _IV_HISTORY_CACHE:
            _IV_HISTORY_CACHE[key] = self._build_iv_history(symbol, 252)
        hist = _IV_HISTORY_CACHE[key]
        today_iv = self._current_atm_iv(symbol)
        below = sum(1 for _, iv in hist if iv <= today_iv)
        return round(100.0 * below / max(len(hist), 1), 1)

    def get_volume_ratio(self, symbol: str) -> float:
        """Today's option volume / 20d avg volume."""
        today = self._today_option_volume(symbol)
        avg = self._avg_option_volume(symbol, 20)
        if avg <= 0:
            return 1.0
        return round(today / avg, 2)

    def get_avg_volume(self, symbol: str) -> float:
        """Average daily option contracts (for min_avg_option_volume filter)."""
        return self._avg_option_volume(symbol, 20)

    def get_put_call_ratio(self) -> float:
        """Aggregate put/call volume across the configured universe."""
        uni = self.cfg.get("universe", {}).get("symbols", ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "MATIC", "DOT"])
        p = c = 0.0
        for s in uni:
            chain = self.get_option_chain(s, 1, 60)
            for _, sides in chain.items():
                for opt in sides.get("puts", []):
                    p += opt.get("volume", 0)
                for opt in sides.get("calls", []):
                    c += opt.get("volume", 0)
        if c <= 0:
            return 1.0
        return round(p / c, 3)

    # ---------- option chain (core method used by strategies) ----------

    def get_option_chain(self, underlying: str, dte_min: int = 0, dte_max: int = 90) -> dict:
        """
        Returns: {expiration_iso_date: {"calls": [...], "puts": [...]}}.
        Filters expirations to those whose days-to-expiry are in [dte_min, dte_max].
        """
        def factory():
            return self._build_option_chain(underlying, dte_min, dte_max)
        return self._cached(f"chain:{underlying}:{dte_min}:{dte_max}", factory)

    def _build_option_chain(self, underlying: str, dte_min: int, dte_max: int) -> dict:
        spot = self.get_last_price(underlying) or 100.0

        # Try live Bybit option chain + tickers. If both come back empty we go synthetic.
        instruments = []
        tickers = []
        try:
            instruments = self.client.get_option_chain_bybit(underlying)
            tickers = self.client.get_option_tickers(underlying)
        except Exception as e:
            log.debug("live chain fetch for %s failed: %s", underlying, e)
            instruments = []

        if instruments and tickers:
            return self._chain_from_live(instruments, tickers, dte_min, dte_max, spot)
        return self._chain_synthetic(underlying, spot, dte_min, dte_max)

    def _chain_from_live(self, instruments: list, tickers: list, dte_min: int, dte_max: int, spot: float) -> dict:
        tick_map = {t.get("symbol"): t for t in tickers}
        out = {}
        for ins in instruments:
            expiry_ts_ms = int(ins.get("deliveryTime", 0))
            if not expiry_ts_ms:
                continue
            expiry = datetime.utcfromtimestamp(expiry_ts_ms / 1000).date()
            dte = (expiry - date.today()).days
            if dte < dte_min or dte > dte_max:
                continue
            expiry_str = expiry.isoformat()
            side = "calls" if str(ins.get("optionsType", "")).lower() == "call" else "puts"
            sym = ins.get("symbol", "")
            strike = float(ins.get("strikePrice", 0) or 0)
            if strike <= 0:
                continue
            tk = tick_map.get(sym, {})
            bid = float(tk.get("bid1Price", 0) or 0)
            ask = float(tk.get("ask1Price", 0) or 0)
            mark = float(tk.get("markPrice", 0) or 0)
            if mark == 0 and bid > 0 and ask > 0:
                mark = round((bid + ask) / 2, 4)
            delta = float(tk.get("delta", 0) or 0)
            gamma = float(tk.get("gamma", 0) or 0)
            vega = float(tk.get("vega", 0) or 0)
            theta = float(tk.get("theta", 0) or 0)
            vol = int(tk.get("turnover24h", 0) or 0)
            oi = float(tk.get("openInterest", 0) or 0)

            if mark <= 0:
                # Black-Scholes rough fallback so the entry isn't dropped
                mark = self._bs_price(spot, strike, max(dte, 1) / 365.0, 0.05, 0.6, side == "calls")
                bid = round(max(mark * 0.95, 0.01), 4)
                ask = round(mark * 1.05, 4)

            out.setdefault(expiry_str, {"calls": [], "puts": []})[side].append({
                "symbol": sym,
                "strike": strike,
                "bid": bid,
                "ask": ask,
                "mark": mark,
                "delta": delta,
                "gamma": gamma,
                "vega": vega,
                "theta": theta,
                "volume": vol,
                "openInterest": oi,
            })
        return out

    def _chain_synthetic(self, underlying: str, spot: float, dte_min: int, dte_max: int) -> dict:
        rng = random.Random(hash(f"{underlying}-{self._seed_day}-chain") & 0xFFFFFFFF)
        iv = self._current_atm_iv(underlying) / 100.0
        today = date.today()
        expiry_candidates = []
        # generate weekly-ish expiries inside the dte window
        step_days = 7 if dte_max - dte_min > 20 else 1
        for d in range(dte_min, max(dte_max + 1, dte_min + 1), step_days):
            expiry_candidates.append(today + timedelta(days=d))
        if not expiry_candidates:
            expiry_candidates = [today + timedelta(days=max(dte_min, 7))]

        out = {}
        # strike ladder around spot, +/- ~30% in 2.5% steps
        strikes = []
        step_pct = 0.025
        for k in range(-12, 13):
            strikes.append(round(spot * (1.0 + k * step_pct), 2))
        strikes = sorted(set(strikes))

        for expiry in expiry_candidates:
            dte = (expiry - today).days
            t = max(dte, 1) / 365.0
            expiry_str = expiry.isoformat()
            calls = []
            puts = []
            for strike in strikes:
                call_price = self._bs_price(spot, strike, t, 0.05, iv, True)
                put_price = self._bs_price(spot, strike, t, 0.05, iv, False)
                spread = max(call_price * 0.03, spot * 0.0005)
                call_bid = round(max(call_price - spread, 0.01), 4)
                call_ask = round(call_price + spread, 4)
                put_bid = round(max(put_price - spread, 0.01), 4)
                put_ask = round(put_price + spread, 4)
                delta = self._bs_delta(spot, strike, t, 0.05, iv, True)
                jitter = 0.7 + rng.random() * 0.6
                vol = int(100 * jitter * max(0.1, 1.0 - abs(strike - spot) / spot * 3))
                calls.append({
                    "symbol": f"{underlying}-{expiry_str}-{strike}-C",
                    "strike": strike,
                    "bid": call_bid, "ask": call_ask,
                    "mark": round((call_bid + call_ask) / 2, 4),
                    "delta": delta,
                    "gamma": self._bs_gamma(spot, strike, t, 0.05, iv),
                    "vega": self._bs_vega(spot, strike, t, 0.05, iv),
                    "theta": self._bs_theta(spot, strike, t, 0.05, iv, True),
                    "volume": vol,
                    "openInterest": int(vol * 5),
                })
                puts.append({
                    "symbol": f"{underlying}-{expiry_str}-{strike}-P",
                    "strike": strike,
                    "bid": put_bid, "ask": put_ask,
                    "mark": round((put_bid + put_ask) / 2, 4),
                    "delta": delta - 1.0,
                    "gamma": self._bs_gamma(spot, strike, t, 0.05, iv),
                    "vega": self._bs_vega(spot, strike, t, 0.05, iv),
                    "theta": self._bs_theta(spot, strike, t, 0.05, iv, False),
                    "volume": vol,
                    "openInterest": int(vol * 5),
                })
            out[expiry_str] = {"calls": calls, "puts": puts}
        return out

    # ---------- synthetic internals ----------

    def _build_iv_history(self, symbol: str, n_days: int) -> list[tuple[str, float]]:
        rng = random.Random(hash(f"ivhist-{symbol}") & 0xFFFFFFFF)
        today = date.today()
        s = symbol.upper()
        if s == "BTC":
            base_iv = 55.0
        elif s == "ETH":
            base_iv = 60.0
        elif s in ("SOL", "BNB", "AVAX", "MATIC"):
            base_iv = 65.0
        else:
            base_iv = 70.0
        hist = []
        for i in range(n_days):
            d = today - timedelta(days=n_days - i)
            iv = base_iv + rng.uniform(-20, 15)
            hist.append((d.isoformat(), round(iv, 2)))
        return hist

    def _current_atm_iv(self, symbol: str) -> float:
        seed = hash(f"{symbol}-{self._seed_day}-iv") % 1000
        s = symbol.upper()
        if s == "BTC":
            base = 55.0
        elif s == "ETH":
            base = 60.0
        elif s in ("SOL", "BNB", "AVAX", "MATIC"):
            base = 65.0
        else:
            base = 70.0
        return round(base + (seed % 30) - 10, 2)

    def _today_option_volume(self, symbol: str) -> float:
        rng = random.Random(hash(f"{symbol}-{self._seed_day}-vol") & 0xFFFFFFFF)
        s = symbol.upper()
        if s == "BTC":
            base = 5000
        elif s == "ETH":
            base = 4000
        elif s == "SOL":
            base = 3000
        elif s == "BNB":
            base = 2000
        elif s == "AVAX":
            base = 1500
        elif s in ("XRP", "DOGE"):
            base = 1200
        else:
            base = 1000
        return round(base * (0.5 + rng.random()))

    def _avg_option_volume(self, symbol: str, days: int) -> float:
        rng = random.Random(hash(f"{symbol}-avgvol-{days}") & 0xFFFFFFFF)
        s = symbol.upper()
        if s == "BTC":
            base = 5000
        elif s == "ETH":
            base = 4000
        elif s == "SOL":
            base = 3000
        elif s == "BNB":
            base = 2000
        elif s == "AVAX":
            base = 1500
        elif s in ("XRP", "DOGE"):
            base = 1200
        else:
            base = 1000
        return round(base * (0.7 + rng.random() * 0.3))

    def _synthetic_bars(self, symbol: str, n: int) -> list[dict]:
        rng = random.Random(hash(f"bars-{symbol}-{n}") & 0xFFFFFFFF)
        spot = self.get_last_price(symbol) or 100.0
        s = symbol.upper()
        if s == "BTC":
            vol = 0.025
        elif s == "ETH":
            vol = 0.030
        elif s in ("SOL", "BNB", "AVAX", "MATIC"):
            vol = 0.038
        else:
            vol = 0.045
        bars = []
        px = spot
        for i in range(n):
            drift = 0.0003
            ret = drift + rng.gauss(0, vol)
            px = px * (1 + ret)
            o = px * (1 + rng.gauss(0, vol / 2))
            h = px * (1 + abs(rng.gauss(0, vol / 2)))
            l = px * (1 - abs(rng.gauss(0, vol / 2)))
            bars.append({"open": o, "high": max(o, h, l, px),
                         "low": min(o, h, l, px), "close": px})
        return bars

    # ---------- simple Black-Scholes (for synthetic chain + fallback) ----------

    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _d1d2(self, S, K, T, r, sigma):
        if T <= 0 or sigma <= 0:
            return 0.0, 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return d1, d2

    def _bs_price(self, S, K, T, r, sigma, is_call: bool) -> float:
        if T <= 0:
            return max(0.0, (S - K) if is_call else (K - S))
        d1, d2 = self._d1d2(S, K, T, r, sigma)
        if is_call:
            return round(S * self._norm_cdf(d1) - K * math.exp(-r * T) * self._norm_cdf(d2), 4)
        return round(K * math.exp(-r * T) * self._norm_cdf(-d2) - S * self._norm_cdf(-d1), 4)

    def _bs_delta(self, S, K, T, r, sigma, is_call: bool) -> float:
        if T <= 0:
            return 1.0 if (S > K) == is_call else 0.0
        d1, _ = self._d1d2(S, K, T, r, sigma)
        return self._norm_cdf(d1) if is_call else self._norm_cdf(d1) - 1.0

    def _bs_gamma(self, S, K, T, r, sigma) -> float:
        if T <= 0 or sigma <= 0:
            return 0.0
        d1, _ = self._d1d2(S, K, T, r, sigma)
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        return pdf / (S * sigma * math.sqrt(T))

    def _bs_vega(self, S, K, T, r, sigma) -> float:
        if T <= 0 or sigma <= 0:
            return 0.0
        d1, _ = self._d1d2(S, K, T, r, sigma)
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        return S * math.sqrt(T) * pdf * 0.01

    def _bs_theta(self, S, K, T, r, sigma, is_call: bool) -> float:
        if T <= 0:
            return 0.0
        d1, d2 = self._d1d2(S, K, T, r, sigma)
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        term1 = -S * pdf * sigma / (2 * math.sqrt(T))
        if is_call:
            term2 = -r * K * math.exp(-r * T) * self._norm_cdf(d2)
        else:
            term2 = r * K * math.exp(-r * T) * self._norm_cdf(-d2)
        return (term1 + term2) / 365.0
