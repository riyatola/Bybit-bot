"""
Sentiment overlay: reweights candidate scores using two signal layers —
a slow-moving macro regime and event-driven per-symbol news sentiment.

Same contract as learning.py: this only REWEIGHTS candidates the strategy
modules already produced. It never invents a trade and never overrides
risk_manager's hard caps. Integration point in scanner.py:

    scan -> candidates -> learning.apply() -> sentiment.apply() -> risk.gate() -> approval

Two distinct signal layers, per the "macro sets the weather, news is
noise unless it changes the macro read" principle:

  1. Macro regime (RISK_ON / NEUTRAL / RISK_OFF) — composite of:
       - VIX level/percentile: FRED `VIXCLS`, refined with a live Schwab
         quote on $VIX.X when a market_data adapter is wired in (FRED's
         VIXCLS lags by up to a day).
       - 10y-2y yield curve slope: FRED `DGS10` - `DGS2`. Inversion
         (<=0) leans risk-off.
       - High-yield credit spread *change*: FRED `BAMLH0A0HYM2`,
         day-over-day delta. Widening leans risk-off, tightening leans
         risk-on.
     Majority vote across the three inputs gives a *raw* daily regime.
     A regime only takes effect once `persistence_days` consecutive raw
     readings agree — this is the noise filter. A single noisy day (one
     VIX spike, one data revision) can't flip strategy sizing on its own.

  2. Per-symbol news sentiment — Finnhub's `/news-sentiment` endpoint,
     reusing the same API key as earnings.py (earnings.finnhub_api_key /
     FINNHUB_API_KEY). Left alone inside a normal range; once it crosses
     `news_extreme_threshold` it gets faded *toward* neutral, never
     amplified — sentiment is a well-known contrarian indicator at
     extremes, so this dampens over-crowded reads rather than chasing them.

Regime state is persisted in the same SQLite file as everything else
(approval_manager.Db) so persistence-day confirmation survives restarts.
"""

import logging
import os
import time
from datetime import date, timedelta

import requests

log = logging.getLogger("sentiment")

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FINNHUB_BASE = "https://finnhub.io/api/v1"

DEFAULTS = {
    "enabled": True,
    "persistence_days": 3,           # consecutive agreeing raw readings needed to confirm a regime
    "vix_lookback_days": 252,
    "vix_high_percentile": 80,       # VIX percentile >= this -> risk-off vote
    "vix_low_percentile": 20,        # VIX percentile <= this -> risk-on vote
    "curve_inversion_bps": 0,        # 10y-2y <= this (bps) -> risk-off vote
    "credit_spread_widen_bps": 20,   # day/day widening >= this (bps) -> risk-off vote
    "credit_spread_tighten_bps": -10,  # day/day tightening <= this (bps) -> risk-on vote
    # -- new macro inputs --
    "breakeven_high_pct": 3.0,       # TIPS breakeven >= this -> risk-off vote (inflation fear)
    "breakeven_low_pct": 1.5,        # TIPS breakeven <= this -> risk-off vote (deflation/weak growth fear)
    "surprise_index_high": 50,       # Citi Surprise >= this -> risk-on vote
    "surprise_index_low": -50,       # Citi Surprise <= this -> risk-off vote
    "put_call_ratio_high": 1.2,      # Put/call ratio >= this -> risk-off vote (fear)
    "put_call_ratio_low": 0.7,       # Put/call ratio <= this -> risk-on vote (greed)
    "news_extreme_threshold": 0.8,   # |Finnhub companyNewsScore| >= this counts as "extreme"
    "contrarian_dampening_factor": 0.5,  # how hard extreme readings get faded toward neutral
    # -- directional-only volatility band (orthogonal to the RISK_ON/OFF regime above) --
    # Level-based (not percentile-based): VIX too low often means a
    # complacent, range-bound tape where breakouts tend to be false
    # signals; VIX too high often means a chaotic/crisis tape where
    # whipsaws and wide spreads work against a long-premium breakout too.
    # A candidate can be RISK_ON by the composite regime vote AND still
    # sit outside this band (e.g. VIX at 12 with a steep curve) — both
    # factors apply independently and multiply through.
    "directional_vix_min": 15.0,
    "directional_vix_max": 25.0,
    "directional_vix_band_multiplier": 0.0,  # applied to directional score when VIX level is outside [min, max]
    "risk_off_multipliers": {
        "directional": 0.0,     # zero out directional/long-premium strategies
        "earnings_vol": 0.0,    # long straddles are long-premium too
        "credit_spreads": 0.5,  # defined-risk: halve size, don't zero
        "wheel": 0.5,
    },
    "risk_on_multipliers": {
        "directional": 1.15,    # modest boost
        "earnings_vol": 1.15,
        "credit_spreads": 1.05,
        "wheel": 1.05,
    },
    "neutral_multipliers": {
        "directional": 1.0,
        "earnings_vol": 1.0,
        "credit_spreads": 1.0,
        "wheel": 1.0,
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_regime_history (
    date TEXT PRIMARY KEY,
    vix_level REAL,
    vix_percentile REAL,
    curve_slope_bps REAL,
    credit_spread REAL,
    credit_spread_delta_bps REAL,
    tips_breakeven_pct REAL,     -- new: 10y TIPS breakeven inflation rate
    citi_surprise_index REAL,    -- new: Citi Economic Surprise Index for US
    put_call_ratio REAL,         -- new: put/call ratio from Schwab
    raw_regime TEXT,          -- today's regime before persistence confirmation
    confirmed_regime TEXT     -- regime actually applied that day, after persistence check
);
"""


def _merge_defaults(user_cfg: dict) -> dict:
    merged = {**DEFAULTS, **user_cfg}
    for key in ("risk_off_multipliers", "risk_on_multipliers", "neutral_multipliers"):
        merged[key] = {**DEFAULTS[key], **user_cfg.get(key, {})}
    return merged


class FredClient:
    """Thin wrapper around FRED's series/observations endpoint. Sign up for
    a free key at https://fred.stlouisfed.org/docs/api/api_key.html and set
    fred.api_key in config.yaml (or FRED_API_KEY env var)."""

    _CACHE_TTL_SECONDS = 6 * 3600

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._cache = {}  # series_id -> (fetched_at, [(date, value), ...])
        # Set up session with retries
        self.session = requests.Session()
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)

    def series(self, series_id: str, lookback_days: int = 400) -> list[tuple[str, float]]:
        cached = self._cache.get(series_id)
        if cached and (time.time() - cached[0]) < self._CACHE_TTL_SECONDS:
            return cached[1]
        if not self.api_key:
            return cached[1] if cached else []

        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": (date.today() - timedelta(days=lookback_days)).isoformat(),
        }
        try:
            resp = self.session.get(FRED_BASE, params=params, timeout=30)
            resp.raise_for_status()
            raw_obs = resp.json().get("observations", [])
        except Exception as e:
            log.warning("FRED fetch failed for %s: %s", series_id, e)
            return cached[1] if cached else []

        parsed = []
        for o in raw_obs:
            try:
                parsed.append((o["date"], float(o["value"])))
            except (ValueError, KeyError):
                continue  # FRED uses "." for missing observations

        self._cache[series_id] = (time.time(), parsed)
        return parsed

    def latest(self, series_id: str, lookback_days: int = 400):
        obs = self.series(series_id, lookback_days)
        return obs[-1] if obs else None


class HistoricalRegimeEngine:
    """Date-parameterized, lookahead-safe macro regime reconstruction for
    backtesting (see backtest_engine.py's use_sentiment option).

    SentimentEngine (below) is a *live* engine: current_regime() always
    answers "what is the regime today", using whatever FRED prints are in
    cache right now. Naively calling that inside a backtest loop over
    e.g. 2024 dates would score January 2024 candidates using July 2026's
    VIX/curve/credit reading — a severe lookahead leak, the same class of
    bug AUDIT_FINDINGS.md #8 already fixed for learning.py's ML layer via
    LearningEngine.maybe_retrain_historical.

    This class fixes that: it pulls each FRED series ONCE (a wide window
    covering the whole backtest run plus enough lead time for the vix
    lookback + persistence check), then for any given `day` computes the
    raw regime using ONLY observations dated <= day, and confirms it via
    the same N-day persistence rule as SentimentEngine, walking backward
    from `day` using only earlier raw regimes. No per-symbol news
    sentiment is reproduced here — Finnhub's /news-sentiment endpoint has
    no historical time series available, so the backtest sentiment
    overlay is macro-only (regime multiplier), never the news dampening
    factor. That's a known, documented gap, not an oversight.
    """

    def __init__(self, cfg: dict, fred_api_key: str):
        self.cfg = _merge_defaults(cfg.get("sentiment", {}))
        self.fred = FredClient(fred_api_key)
        self._raw_cache = {}     # date_iso -> raw regime string
        self._confirmed_cache = {}  # date_iso -> confirmed regime string
        self._loaded = False
        self._vix_obs = []
        self._curve_dgs10 = []
        self._curve_dgs2 = []
        self._credit_obs = []
        self._tips_breakeven_obs = []
        self._citi_surprise_obs = []

    def preload(self, start_date, end_date):
        """Fetch each series once, covering [start_date - vix_lookback_days,
        end_date]. Call this before the backtest loop starts."""
        lookback_days = self.cfg["vix_lookback_days"] + self.cfg["persistence_days"] + 30
        span_days = (end_date - start_date).days + lookback_days
        self._vix_obs = self.fred.series("VIXCLS", lookback_days=span_days)
        self._curve_dgs10 = self.fred.series("DGS10", lookback_days=span_days)
        self._curve_dgs2 = self.fred.series("DGS2", lookback_days=span_days)
        self._credit_obs = self.fred.series("BAMLH0A0HYM2", lookback_days=span_days)
        self._tips_breakeven_obs = self.fred.series("T10YIE", lookback_days=span_days)
        self._citi_surprise_obs = self.fred.series("CESIUSD", lookback_days=span_days)
        self._loaded = True
        if not (self._vix_obs and self._curve_dgs10 and self._curve_dgs2 and self._credit_obs):
            log.warning(
                "HistoricalRegimeEngine: one or more core FRED series returned no data "
                "(no FRED API key configured, or network unavailable). Backtest "
                "sentiment overlay will fall back to NEUTRAL for the whole run."
            )

    @staticmethod
    def _as_of(obs, day_iso):
        """Most recent (date, value) in obs with date <= day_iso, or None."""
        candidates = [(d, v) for d, v in obs if d <= day_iso]
        return candidates[-1] if candidates else None

    def _raw_regime_on(self, day) -> str:
        day_iso = day.isoformat()
        if day_iso in self._raw_cache:
            return self._raw_cache[day_iso]

        vix_window = [(d, v) for d, v in self._vix_obs if d <= day_iso][-self.cfg["vix_lookback_days"]:]
        ten = self._as_of(self._curve_dgs10, day_iso)
        two = self._as_of(self._curve_dgs2, day_iso)
        credit_window = [(d, v) for d, v in self._credit_obs if d <= day_iso][-2:]
        tips_breakeven = self._as_of(self._tips_breakeven_obs, day_iso)
        citi_surprise = self._as_of(self._citi_surprise_obs, day_iso)

        if len(vix_window) < 20 or not ten or not two or len(credit_window) < 2:
            raw = "NEUTRAL"
            self._raw_cache[day_iso] = raw
            return raw

        vix_values = [v for _, v in vix_window]
        current_vix = vix_values[-1]
        vix_pct = 100 * (sum(1 for v in vix_values if v <= current_vix) / len(vix_values))
        curve_bps = round((ten[1] - two[1]) * 100, 1)
        credit_delta_bps = round((credit_window[-1][1] - credit_window[-2][1]) * 100, 1)

        risk_off_votes = risk_on_votes = 0
        if vix_pct >= self.cfg["vix_high_percentile"]:
            risk_off_votes += 1
        elif vix_pct <= self.cfg["vix_low_percentile"]:
            risk_on_votes += 1

        if curve_bps <= self.cfg["curve_inversion_bps"]:
            risk_off_votes += 1
        else:
            risk_on_votes += 1

        if credit_delta_bps >= self.cfg["credit_spread_widen_bps"]:
            risk_off_votes += 1
        elif credit_delta_bps <= self.cfg["credit_spread_tighten_bps"]:
            risk_on_votes += 1

        if tips_breakeven is not None:
            if tips_breakeven[1] >= self.cfg["breakeven_high_pct"] or tips_breakeven[1] <= self.cfg["breakeven_low_pct"]:
                risk_off_votes += 1

        if citi_surprise is not None:
            if citi_surprise[1] >= self.cfg["surprise_index_high"]:
                risk_on_votes += 1
            elif citi_surprise[1] <= self.cfg["surprise_index_low"]:
                risk_off_votes += 1

        if risk_off_votes > risk_on_votes:
            raw = "RISK_OFF"
        elif risk_on_votes > risk_off_votes:
            raw = "RISK_ON"
        else:
            raw = "NEUTRAL"
        self._raw_cache[day_iso] = raw
        return raw

    def regime_on(self, day) -> str:
        """Confirmed regime for `day`, using only raw regimes computed
        from data available on or before `day` (and, for the persistence
        window, on or before each of the `persistence_days` days walking
        backward from `day` — never anything later)."""
        if not self._loaded:
            raise RuntimeError("Call preload(start_date, end_date) before regime_on().")
        day_iso = day.isoformat()
        if day_iso in self._confirmed_cache:
            return self._confirmed_cache[day_iso]

        from datetime import timedelta as _td
        n = self.cfg["persistence_days"]
        raw_values = []
        cursor = day
        # Walk backward over the last n *calendar* days ending at `day`
        # (good enough for a daily-bar backtest; weekends just repeat the
        # prior trading day's as-of read, same as the raw computation
        # naturally handles via _as_of's <= day_iso lookup).
        for i in range(n):
            raw_values.append(self._raw_regime_on(cursor - _td(days=i)))

        if len(raw_values) < n or any(v is None for v in raw_values):
            confirmed = "NEUTRAL"
        elif all(v == raw_values[0] for v in raw_values):
            confirmed = raw_values[0]
        else:
            confirmed = "NEUTRAL"

        self._confirmed_cache[day_iso] = confirmed
        return confirmed

    def multiplier_for(self, strategy: str, day) -> tuple:
        """Returns (regime, multiplier) for a strategy on `day` — the
        macro-only half of SentimentEngine.apply() (no news dampening,
        no directional VIX band — see class docstring)."""
        regime = self.regime_on(day)
        regime_key = {
            "RISK_OFF": "risk_off_multipliers",
            "RISK_ON": "risk_on_multipliers",
            "NEUTRAL": "neutral_multipliers",
        }.get(regime, "neutral_multipliers")
        return regime, self.cfg[regime_key].get(strategy, 1.0)


class SentimentEngine:
    def __init__(self, cfg: dict, db, market_data=None):
        self.cfg = _merge_defaults(cfg.get("sentiment", {}))
        self.db = db
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        # Optional: used to refine the FRED VIX reading with a same-day
        # Schwab live quote on $VIX.X. Works fine without it (falls back
        # to FRED's own last VIXCLS print).
        self.market_data = market_data

        fred_key = cfg.get("fred", {}).get("api_key") or os.environ.get("FRED_API_KEY")
        if not fred_key:
            log.warning(
                "No FRED API key configured (fred.api_key in config.yaml or FRED_API_KEY "
                "env var) — macro regime will default to NEUTRAL until one is set."
            )
        self.fred = FredClient(fred_key)

        # Reuses earnings.py's Finnhub key deliberately — same provider,
        # same free-tier account, one less credential to manage.
        self.finnhub_api_key = (
            cfg.get("earnings", {}).get("finnhub_api_key")
            or os.environ.get("FINNHUB_API_KEY")
        )
        self._news_cache = {}  # symbol -> (fetched_at, score or None)

    # ---------- macro regime: inputs ----------

    def _vix_percentile(self):
        obs = self.fred.series("VIXCLS", self.cfg["vix_lookback_days"])
        if len(obs) < 20:
            return None, None
        values = [v for _, v in obs]
        current = values[-1]

        if self.market_data:
            try:
                live = self.market_data.get_last_price("$VIX.X")
                if live:
                    current = live
            except Exception:
                pass  # FRED's own last print is a fine fallback

        pct = 100 * (sum(1 for v in values if v <= current) / len(values))
        return current, pct

    def _yield_curve_slope_bps(self):
        ten = self.fred.latest("DGS10")
        two = self.fred.latest("DGS2")
        if not ten or not two:
            return None
        return round((ten[1] - two[1]) * 100, 1)  # DGS10/DGS2 in %, diff -> bps

    def _credit_spread_and_delta_bps(self):
        obs = self.fred.series("BAMLH0A0HYM2", lookback_days=30)
        if len(obs) < 2:
            return None, None
        current = obs[-1][1]
        prev = obs[-2][1]
        return current, round((current - prev) * 100, 1)

    def _tips_breakeven_pct(self):
        obs = self.fred.series("T10YIE", lookback_days=30)
        if not obs:
            return None
        return obs[-1][1]

    def _citi_surprise_index(self):
        obs = self.fred.series("CESIUSD", lookback_days=30)
        if not obs:
            return None
        return obs[-1][1]

    def _put_call_ratio(self):
        if not self.market_data:
            return None
        try:
            return self.market_data.get_put_call_ratio()
        except Exception as e:
            log.warning("Failed to get put/call ratio: %s", e)
            return None

    def _raw_regime_today(self):
        vix_level, vix_pct = self._vix_percentile()
        curve_bps = self._yield_curve_slope_bps()
        credit_level, credit_delta_bps = self._credit_spread_and_delta_bps()
        tips_breakeven = self._tips_breakeven_pct()
        citi_surprise = self._citi_surprise_index()
        put_call_ratio = self._put_call_ratio()

        if vix_pct is None or curve_bps is None or credit_delta_bps is None:
            # Missing core data -> NEUTRAL rather than guessing off a partial read.
            return "NEUTRAL", vix_level, vix_pct, curve_bps, credit_level, credit_delta_bps, tips_breakeven, citi_surprise, put_call_ratio

        risk_off_votes = 0
        risk_on_votes = 0

        # Original inputs
        if vix_pct >= self.cfg["vix_high_percentile"]:
            risk_off_votes += 1
        elif vix_pct <= self.cfg["vix_low_percentile"]:
            risk_on_votes += 1

        if curve_bps <= self.cfg["curve_inversion_bps"]:
            risk_off_votes += 1
        else:
            risk_on_votes += 1

        if credit_delta_bps >= self.cfg["credit_spread_widen_bps"]:
            risk_off_votes += 1
        elif credit_delta_bps <= self.cfg["credit_spread_tighten_bps"]:
            risk_on_votes += 1

        # New inputs
        if tips_breakeven is not None:
            if tips_breakeven >= self.cfg["breakeven_high_pct"] or tips_breakeven <= self.cfg["breakeven_low_pct"]:
                risk_off_votes += 1

        if citi_surprise is not None:
            if citi_surprise >= self.cfg["surprise_index_high"]:
                risk_on_votes += 1
            elif citi_surprise <= self.cfg["surprise_index_low"]:
                risk_off_votes += 1

        if put_call_ratio is not None:
            if put_call_ratio >= self.cfg["put_call_ratio_high"]:
                risk_off_votes += 1
            elif put_call_ratio <= self.cfg["put_call_ratio_low"]:
                risk_on_votes += 1

        # Majority of votes: since we have up to 6, need > risk_on (or vice versa)
        if risk_off_votes > risk_on_votes:
            raw = "RISK_OFF"
        elif risk_on_votes > risk_off_votes:
            raw = "RISK_ON"
        else:
            raw = "NEUTRAL"

        return raw, vix_level, vix_pct, curve_bps, credit_level, credit_delta_bps, tips_breakeven, citi_surprise, put_call_ratio

    # ---------- macro regime: persistence + public API ----------

    def refresh_macro_regime(self) -> str:
        """Call once per scan cycle — cheap, since FRED/Schwab reads are
        cached for hours. Records today's raw reading (once per day) and
        returns the *confirmed* regime: the raw regime only takes effect
        once it has matched persistence_days in a row."""
        today = date.today().isoformat()
        raw, vix_level, vix_pct, curve_bps, credit_level, credit_delta_bps, tips_breakeven, citi_surprise, put_call_ratio = self._raw_regime_today()

        already_recorded = self.conn.execute(
            "SELECT 1 FROM macro_regime_history WHERE date=?", (today,)
        ).fetchone()
        if not already_recorded:
            self.conn.execute(
                "INSERT INTO macro_regime_history "
                "(date, vix_level, vix_percentile, curve_slope_bps, credit_spread, "
                "credit_spread_delta_bps, tips_breakeven_pct, citi_surprise_index, put_call_ratio, "
                "raw_regime, confirmed_regime) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (today, vix_level, vix_pct, curve_bps, credit_level, credit_delta_bps, tips_breakeven, citi_surprise, put_call_ratio, raw, None),
            )
            self.conn.commit()

        confirmed = self._confirm_regime()
        self.conn.execute(
            "UPDATE macro_regime_history SET confirmed_regime=? WHERE date=?", (confirmed, today)
        )
        self.conn.commit()
        return confirmed

    def _confirm_regime(self) -> str:
        n = self.cfg["persistence_days"]
        rows = self.conn.execute(
            "SELECT raw_regime FROM macro_regime_history ORDER BY date DESC LIMIT ?", (n,)
        ).fetchall()
        raw_values = [r[0] for r in rows]
        if len(raw_values) < n:
            return "NEUTRAL"  # not enough history yet to confirm a flip
        if all(v == raw_values[0] for v in raw_values):
            return raw_values[0]
        return "NEUTRAL"  # readings disagree within the window -> not persistent, hold NEUTRAL

    def current_regime(self) -> str:
        """Cheap lookup of today's already-computed confirmed regime; computes
        it fresh if refresh_macro_regime() hasn't run yet today."""
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT confirmed_regime FROM macro_regime_history WHERE date=?", (today,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        return self.refresh_macro_regime()

    # ---------- per-symbol news sentiment ----------

    def _raw_news_sentiment(self, symbol: str):
        """Finnhub's /news-sentiment response shape has drifted before
        (sentiment.companyNewsScore vs a top-level companyNewsScore field
        across API versions) — read both defensively and return None
        rather than guessing on anything unexpected."""
        cached = self._news_cache.get(symbol)
        if cached and (time.time() - cached[0]) < 6 * 3600:
            return cached[1]
        if not self.finnhub_api_key:
            return None

        try:
            resp = requests.get(
                f"{FINNHUB_BASE}/news-sentiment",
                params={"symbol": symbol.upper(), "token": self.finnhub_api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Finnhub news-sentiment fetch failed for %s: %s", symbol, e)
            return cached[1] if cached else None

        score = (data.get("sentiment") or {}).get("companyNewsScore")
        if score is None:
            score = data.get("companyNewsScore")

        self._news_cache[symbol] = (time.time(), score)
        return score

    def news_sentiment_multiplier(self, symbol: str) -> float:
        """1.0 = no opinion (missing data, or reading inside the normal
        range — this is deliberate; only extremes get touched). Once
        |score| crosses news_extreme_threshold, the multiplier fades
        toward neutral rather than following the crowd — the contrarian
        guard against acting on an already-crowded read."""
        if not self.cfg["enabled"]:
            return 1.0
        score = self._raw_news_sentiment(symbol)
        if score is None:
            return 1.0

        threshold = self.cfg["news_extreme_threshold"]
        if abs(score) < threshold:
            return 1.0

        excess = abs(score) - threshold
        damp = 1.0 - min(excess, 1.0) * self.cfg["contrarian_dampening_factor"]
        return max(0.5, damp)

    # ---------- directional-only volatility band ----------

    def directional_vix_band_multiplier(self) -> float:
        """Independent of the RISK_ON/NEUTRAL/RISK_OFF composite regime —
        checks whether VIX's current *level* (not percentile) sits inside
        [directional_vix_min, directional_vix_max]. Reuses the same VIX
        read _vix_percentile() already fetches (FRED VIXCLS, refined with
        a live Schwab quote when available) so this doesn't cost an extra
        API call. 1.0 = no opinion (inside the band, or VIX data missing —
        same "missing data means don't touch the score" convention as
        news_sentiment_multiplier). Outside the band, applies
        directional_vix_band_multiplier (0.0 by default — in practice
        this zeroes the score, but same as risk_off_multipliers.directional
        already does today, the candidate still reaches you on Telegram
        with that score shown; this doesn't stop it from being sent, it
        informs the human tap)."""
        if not self.cfg["enabled"]:
            return 1.0
        vix_level, _ = self._vix_percentile()
        if vix_level is None:
            return 1.0
        lo, hi = self.cfg["directional_vix_min"], self.cfg["directional_vix_max"]
        if lo <= vix_level <= hi:
            return 1.0
        return self.cfg["directional_vix_band_multiplier"]

    # ---------- integration point: scanner.py calls this after learning.apply() ----------

    def apply(self, candidate: dict) -> dict:
        """Multiplies candidate['score'] by (macro regime multiplier) x
        (news sentiment multiplier) x (directional VIX-band multiplier,
        directional candidates only). Same contract as learning.apply():
        pure reweighting, never introduces a new candidate, never exceeds
        1.0."""
        if not self.cfg["enabled"]:
            return candidate

        regime = self.current_regime()
        strategy = candidate.get("strategy", "")
        regime_key = {
            "RISK_OFF": "risk_off_multipliers",
            "RISK_ON": "risk_on_multipliers",
            "NEUTRAL": "neutral_multipliers",
        }.get(regime, "neutral_multipliers")
        regime_mult = self.cfg[regime_key].get(strategy, 1.0)

        news_mult = self.news_sentiment_multiplier(candidate.get("symbol", ""))

        vix_band_mult = 1.0
        if strategy == "directional":
            vix_band_mult = self.directional_vix_band_multiplier()

        base_score = candidate.get("score", 0.5)
        adjusted = base_score * regime_mult * news_mult * vix_band_mult

        candidate["score_before_sentiment"] = round(base_score, 3)
        candidate["macro_regime"] = regime
        candidate["regime_multiplier"] = round(regime_mult, 3)
        candidate["news_sentiment_multiplier"] = round(news_mult, 3)
        if strategy == "directional":
            candidate["vix_band_multiplier"] = round(vix_band_mult, 3)
        candidate["score"] = round(max(0.0, min(1.0, adjusted)), 3)
        return candidate

    # ---------- reporting ----------

    def summarize(self) -> str:
        """Human-readable macro regime block for the weekly Telegram report
        (see scanner.run_learning_cycle)."""
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT vix_level, vix_percentile, curve_slope_bps, credit_spread, "
            "credit_spread_delta_bps, tips_breakeven_pct, citi_surprise_index, put_call_ratio, "
            "raw_regime, confirmed_regime "
            "FROM macro_regime_history WHERE date=?", (today,)
        ).fetchone()
        if not row:
            return "*Macro regime*: no reading recorded yet today."

        vix_level, vix_pct, curve_bps, credit_level, credit_delta_bps, tips_breakeven, citi_surprise, put_call_ratio, raw, confirmed = row
        lines = [f"*Macro regime*: {confirmed or 'NEUTRAL'} (today's raw reading: {raw})"]
        if vix_level is not None:
            lines.append(f"• VIX {vix_level:.1f} ({vix_pct:.0f}th percentile, "
                          f"{self.cfg['vix_lookback_days']}d lookback)")
        if curve_bps is not None:
            lines.append(f"• 10y-2y slope: {curve_bps:+.0f} bps")
        if credit_delta_bps is not None and credit_level is not None:
            lines.append(f"• HY credit spread {credit_level:.2f} ({credit_delta_bps:+.0f} bps day/day)")
        if tips_breakeven is not None:
            lines.append(f"• 10y TIPS breakeven: {tips_breakeven:.2f}%")
        if citi_surprise is not None:
            lines.append(f"• Citi Economic Surprise Index: {citi_surprise:.1f}")
        if put_call_ratio is not None:
            lines.append(f"• SPX put/call ratio: {put_call_ratio:.2f}")
        lines.append(f"• Confirmed via {self.cfg['persistence_days']}-day persistence check")
        if vix_level is not None:
            lo, hi = self.cfg["directional_vix_min"], self.cfg["directional_vix_max"]
            in_band = lo <= vix_level <= hi
            band_mult = 1.0 if in_band else self.cfg["directional_vix_band_multiplier"]
            lines.append(f"• Directional VIX band [{lo:.0f}, {hi:.0f}]: "
                         f"{'in band' if in_band else 'OUTSIDE band'} ({band_mult:.2f}x applied)")
        return "\n".join(lines)