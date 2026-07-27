"""
Bybit-derived alpha overlay: reweights candidate scores using three public
signal layers from Bybit market data:

  1. Long/short account ratio — crowd positioning on linear perps
  2. Funding rate + open interest — carry/crowding regime
  3. Options put/call OI skew — positioning in the options book

Same contract as sentiment.py / learning.py: pure reweighting only.
Integration point in scanner.py:

    scan -> candidates -> learning.apply() -> bybit_alpha.apply() -> sentiment.apply() -> risk.gate()

Missing data always yields multiplier 1.0. Extreme crowding is faded
(contrarian), not chased. Signals require persistence_readings consecutive
agreement before the full multiplier applies.
"""

import logging
import time
from datetime import date

log = logging.getLogger("bybit_alpha")

DEFAULTS = {
    "enabled": True,
    "cache_ttl_seconds": 900,
    "persistence_readings": 3,
    "long_short": {
        "period": "1h",
        "extreme_long_ratio": 0.65,
        "extreme_short_ratio": 0.35,
    },
    "funding": {
        "high_positive": 0.0003,
        "high_negative": -0.0003,
        "oi_rise_pct": 5.0,
    },
    "options_skew": {
        "dte_min": 7,
        "dte_max": 45,
        "bearish_put_call_oi": 1.25,
        "bullish_put_call_oi": 0.80,
    },
    "multipliers": {
        "directional": {
            "aligned": 1.12,
            "crowded_fade": 0.55,
            "neutral": 1.0,
        },
        "credit_spreads": {
            "high_skew_iv": 1.08,
            "crisis_funding": 0.50,
            "neutral": 1.0,
        },
        "wheel": {
            "extreme_fear": 1.10,
            "extreme_greed": 0.85,
            "neutral": 1.0,
        },
        "earnings_vol": {
            "aligned": 1.10,
            "crowded_fade": 0.60,
            "neutral": 1.0,
        },
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bybit_alpha_history (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    buy_ratio REAL,
    funding_rate REAL,
    open_interest REAL,
    put_call_oi REAL,
    raw_bias TEXT,
    confirmed_bias TEXT,
    PRIMARY KEY (date, symbol)
);
"""


def _merge_defaults(user_cfg: dict) -> dict:
    merged = {**DEFAULTS, **user_cfg}
    for key in ("long_short", "funding", "options_skew", "multipliers"):
        base = DEFAULTS.get(key, {})
        merged[key] = {**base, **user_cfg.get(key, {})}
        if key == "multipliers":
            for strat, strat_defaults in DEFAULTS["multipliers"].items():
                merged["multipliers"][strat] = {
                    **strat_defaults,
                    **user_cfg.get("multipliers", {}).get(strat, {}),
                }
    return merged


def _candidate_bias(candidate: dict) -> str:
    """BULLISH, BEARISH, or NEUTRAL inferred from legs / description."""
    legs = candidate.get("legs") or []
    for leg in legs:
        occ = (leg.get("occ_symbol") or "").upper()
        instr = (leg.get("instruction") or "").upper()
        is_call = occ.endswith("-C") or (occ.endswith("C") and not occ.endswith("PC"))
        is_put = occ.endswith("-P") or occ.endswith("P") or "PUT" in occ
        if is_call and not is_put:
            if "BUY" in instr:
                return "BULLISH"
            if "SELL" in instr:
                return "BEARISH"
        if is_put:
            if "BUY" in instr:
                return "BEARISH"
            if "SELL" in instr:
                return "BULLISH"
    desc = (candidate.get("description") or "").lower()
    if "long call" in desc or "bull" in desc or "breakout" in desc and "put" not in desc:
        return "BULLISH"
    if "long put" in desc or "bear" in desc:
        return "BEARISH"
    return "NEUTRAL"


class BybitAlphaEngine:
    def __init__(self, cfg: dict, db, client, market_data):
        self.cfg = _merge_defaults(cfg.get("bybit_alpha", {}))
        self.client = client
        self.market_data = market_data
        self.db = db
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._cache = {}  # symbol -> (fetched_at, signal_dict)
        self._prev_oi = {}  # symbol -> last open interest snapshot

    # ---------- data fetch ----------

    def _fetch_signals(self, symbol: str) -> dict:
        cached = self._cache.get(symbol)
        ttl = self.cfg["cache_ttl_seconds"]
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]

        sym = symbol.upper()
        ls_cfg = self.cfg["long_short"]
        buy_ratio = sell_ratio = None
        ls_rows = []
        if hasattr(self.client, "get_long_short_ratio"):
            ls_rows = self.client.get_long_short_ratio(sym, ls_cfg["period"], limit=3)
        if ls_rows:
            latest = ls_rows[0]
            buy_ratio = float(latest.get("buyRatio", 0) or 0)
            sell_ratio = float(latest.get("sellRatio", 0) or 0)

        funding_rate = open_interest = None
        ticker = None
        if hasattr(self.client, "get_linear_ticker"):
            ticker = self.client.get_linear_ticker(sym)
        if ticker:
            funding_rate = float(ticker.get("fundingRate", 0) or 0)
            open_interest = float(ticker.get("openInterest", 0) or 0)

        oi_change_pct = None
        if open_interest is not None:
            prev = self._prev_oi.get(sym)
            if prev and prev > 0:
                oi_change_pct = round(100.0 * (open_interest - prev) / prev, 2)
            self._prev_oi[sym] = open_interest

        skew_cfg = self.cfg["options_skew"]
        put_oi = call_oi = 0.0
        chain = self.market_data.get_option_chain(
            sym, skew_cfg["dte_min"], skew_cfg["dte_max"]
        )
        for _, sides in chain.items():
            for opt in sides.get("puts", []):
                put_oi += float(opt.get("openInterest", 0) or 0)
            for opt in sides.get("calls", []):
                call_oi += float(opt.get("openInterest", 0) or 0)
        put_call_oi = round(put_oi / call_oi, 3) if call_oi > 0 else None

        raw_bias = self._compute_raw_bias(
            buy_ratio, funding_rate, oi_change_pct, put_call_oi
        )
        signals = {
            "buy_ratio": buy_ratio,
            "sell_ratio": sell_ratio,
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "oi_change_pct": oi_change_pct,
            "put_call_oi": put_call_oi,
            "raw_bias": raw_bias,
        }
        self._cache[symbol] = (time.time(), signals)
        return signals

    def _compute_raw_bias(
        self,
        buy_ratio: float | None,
        funding_rate: float | None,
        oi_change_pct: float | None,
        put_call_oi: float | None,
    ) -> str:
        votes = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
        ls = self.cfg["long_short"]
        fund = self.cfg["funding"]
        skew = self.cfg["options_skew"]

        if buy_ratio is not None:
            if buy_ratio >= ls["extreme_long_ratio"]:
                votes["BEARISH"] += 1
            elif buy_ratio <= ls["extreme_short_ratio"]:
                votes["BULLISH"] += 1
            else:
                votes["NEUTRAL"] += 1

        if funding_rate is not None:
            rising_oi = oi_change_pct is not None and oi_change_pct >= fund["oi_rise_pct"]
            if funding_rate >= fund["high_positive"]:
                votes["BEARISH"] += 2 if rising_oi else 1
            elif funding_rate <= fund["high_negative"]:
                votes["BULLISH"] += 2 if rising_oi else 1
            else:
                votes["NEUTRAL"] += 1

        if put_call_oi is not None:
            if put_call_oi >= skew["bearish_put_call_oi"]:
                votes["BEARISH"] += 1
            elif put_call_oi <= skew["bullish_put_call_oi"]:
                votes["BULLISH"] += 1
            else:
                votes["NEUTRAL"] += 1

        if not any(votes.values()):
            return "NEUTRAL"
        return max(votes, key=votes.get)

    # ---------- persistence ----------

    def refresh_signals(self, symbols: list[str]) -> None:
        """Call once per scan cycle — records today's raw bias per symbol."""
        today = date.today().isoformat()
        for symbol in symbols:
            sig = self._fetch_signals(symbol)
            row = self.conn.execute(
                "SELECT 1 FROM bybit_alpha_history WHERE date=? AND symbol=?",
                (today, symbol.upper()),
            ).fetchone()
            if not row:
                self.conn.execute(
                    "INSERT INTO bybit_alpha_history "
                    "(date, symbol, buy_ratio, funding_rate, open_interest, put_call_oi, "
                    "raw_bias, confirmed_bias) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        today,
                        symbol.upper(),
                        sig.get("buy_ratio"),
                        sig.get("funding_rate"),
                        sig.get("open_interest"),
                        sig.get("put_call_oi"),
                        sig.get("raw_bias"),
                        None,
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE bybit_alpha_history SET buy_ratio=?, funding_rate=?, "
                    "open_interest=?, put_call_oi=?, raw_bias=? WHERE date=? AND symbol=?",
                    (
                        sig.get("buy_ratio"),
                        sig.get("funding_rate"),
                        sig.get("open_interest"),
                        sig.get("put_call_oi"),
                        sig.get("raw_bias"),
                        today,
                        symbol.upper(),
                    ),
                )
            confirmed = self._confirm_bias(symbol.upper())
            self.conn.execute(
                "UPDATE bybit_alpha_history SET confirmed_bias=? WHERE date=? AND symbol=?",
                (confirmed, today, symbol.upper()),
            )
        self.conn.commit()

    def _confirm_bias(self, symbol: str) -> str:
        n = self.cfg["persistence_readings"]
        rows = self.conn.execute(
            "SELECT raw_bias FROM bybit_alpha_history WHERE symbol=? "
            "ORDER BY date DESC LIMIT ?",
            (symbol, n),
        ).fetchall()
        values = [r[0] for r in rows]
        if len(values) < n:
            return "NEUTRAL"
        if all(v == values[0] for v in values):
            return values[0]
        return "NEUTRAL"

    def confirmed_bias(self, symbol: str) -> str:
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT confirmed_bias FROM bybit_alpha_history "
            "WHERE date=? AND symbol=?",
            (today, symbol.upper()),
        ).fetchone()
        if row and row[0]:
            return row[0]
        return self._confirm_bias(symbol.upper())

    # ---------- multipliers ----------

    def _ls_multiplier(self, signals: dict, candidate_bias: str, strategy: str) -> float:
        buy_ratio = signals.get("buy_ratio")
        if buy_ratio is None:
            return 1.0
        ls = self.cfg["long_short"]
        m = self.cfg["multipliers"].get(strategy, {})
        if buy_ratio >= ls["extreme_long_ratio"]:
            if candidate_bias == "BULLISH":
                return m.get("crowded_fade", 0.55)
            if candidate_bias == "BEARISH":
                return m.get("aligned", 1.12)
        if buy_ratio <= ls["extreme_short_ratio"]:
            if candidate_bias == "BEARISH":
                return m.get("crowded_fade", 0.55)
            if candidate_bias == "BULLISH":
                return m.get("aligned", 1.12)
        return 1.0

    def _funding_multiplier(self, signals: dict, candidate_bias: str, strategy: str) -> float:
        funding = signals.get("funding_rate")
        if funding is None:
            return 1.0
        fund = self.cfg["funding"]
        m = self.cfg["multipliers"].get(strategy, {})
        oi_rising = (
            signals.get("oi_change_pct") is not None
            and signals["oi_change_pct"] >= fund["oi_rise_pct"]
        )
        if funding >= fund["high_positive"]:
            if strategy == "credit_spreads" and oi_rising:
                return m.get("crisis_funding", 0.50)
            if candidate_bias == "BULLISH":
                return m.get("crowded_fade", 0.55)
            if candidate_bias == "BEARISH":
                return m.get("aligned", 1.12)
        if funding <= fund["high_negative"]:
            if strategy == "wheel":
                return m.get("extreme_fear", 1.10)
            if candidate_bias == "BULLISH":
                return m.get("aligned", 1.12)
            if candidate_bias == "BEARISH":
                return m.get("crowded_fade", 0.55)
        return 1.0

    def _skew_multiplier(self, signals: dict, candidate_bias: str, strategy: str) -> float:
        pcr = signals.get("put_call_oi")
        if pcr is None:
            return 1.0
        skew = self.cfg["options_skew"]
        m = self.cfg["multipliers"].get(strategy, {})
        if pcr >= skew["bearish_put_call_oi"]:
            if candidate_bias == "BEARISH":
                return m.get("aligned", 1.12)
            if candidate_bias == "BULLISH":
                return m.get("crowded_fade", 0.55)
            if strategy == "credit_spreads":
                return m.get("high_skew_iv", 1.08)
        if pcr <= skew["bullish_put_call_oi"]:
            if candidate_bias == "BULLISH":
                return m.get("aligned", 1.12)
            if candidate_bias == "BEARISH":
                return m.get("crowded_fade", 0.55)
            if strategy == "wheel":
                return m.get("extreme_greed", 0.85)
        return 1.0

    def multiplier_for(self, symbol: str, candidate: dict) -> tuple[float, dict]:
        """Returns (combined_multiplier, breakdown_dict)."""
        if not self.cfg["enabled"]:
            return 1.0, {}

        signals = self._fetch_signals(symbol)
        strategy = candidate.get("strategy", "")
        bias = _candidate_bias(candidate)
        confirmed = self.confirmed_bias(symbol)

        ls_m = self._ls_multiplier(signals, bias, strategy)
        fund_m = self._funding_multiplier(signals, bias, strategy)
        skew_m = self._skew_multiplier(signals, bias, strategy)

        combined = ls_m * fund_m * skew_m
        if confirmed == "NEUTRAL" and combined != 1.0:
            combined = 1.0 + (combined - 1.0) * 0.5

        breakdown = {
            "bybit_alpha_bias": confirmed,
            "bybit_alpha_candidate_bias": bias,
            "bybit_alpha_ls_multiplier": round(ls_m, 3),
            "bybit_alpha_funding_multiplier": round(fund_m, 3),
            "bybit_alpha_skew_multiplier": round(skew_m, 3),
            "bybit_alpha_buy_ratio": signals.get("buy_ratio"),
            "bybit_alpha_funding_rate": signals.get("funding_rate"),
            "bybit_alpha_put_call_oi": signals.get("put_call_oi"),
        }
        return round(combined, 3), breakdown

    def apply(self, candidate: dict) -> dict:
        if not self.cfg["enabled"]:
            return candidate
        symbol = candidate.get("symbol", "")
        mult, breakdown = self.multiplier_for(symbol, candidate)
        base = candidate.get("score", 0.5)
        candidate["score_before_bybit_alpha"] = round(base, 3)
        candidate["bybit_alpha_multiplier"] = mult
        candidate.update(breakdown)
        candidate["score"] = round(max(0.0, min(1.0, base * mult)), 3)
        return candidate

    def summarize(self) -> str:
        today = date.today().isoformat()
        rows = self.conn.execute(
            "SELECT symbol, buy_ratio, funding_rate, put_call_oi, raw_bias, confirmed_bias "
            "FROM bybit_alpha_history WHERE date=? ORDER BY symbol",
            (today,),
        ).fetchall()
        if not rows:
            return "*Bybit alpha*: no readings recorded yet today."
        lines = ["*Bybit alpha* (L/S ratio · funding · options skew):"]
        for sym, buy, fund, pcr, raw, confirmed in rows:
            parts = [f"• {sym}: {confirmed or 'NEUTRAL'} (raw {raw})"]
            if buy is not None:
                parts.append(f"L/S long {buy:.0%}")
            if fund is not None:
                parts.append(f"funding {fund:+.4%}")
            if pcr is not None:
                parts.append(f"put/call OI {pcr:.2f}")
            lines.append(" — ".join(parts))
        lines.append(
            f"• Confirmed via {self.cfg['persistence_readings']}-day persistence"
        )
        return "\n".join(lines)
