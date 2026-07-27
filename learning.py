"""
Turns closed-trade outcomes (from trade_tracker.py) into two things:

  1. A rule-based multiplier per (strategy, symbol) and per strategy alone,
     derived from realized win rate and expectancy. This is the primary
     mechanism — simple, auditable, and doesn't need a minimum sample size
     to degrade gracefully (it just returns 1.0x, i.e. "no opinion yet").

  2. An OPTIONAL logistic-regression layer (scikit-learn) that learns from
     the same feature snapshot recorded at entry (features_json in trades)
     to predict win probability, and blends that into the score once
     there's enough data to fit anything meaningful. This is off by
     default (`learning.use_ml: false` in config.yaml) — turn it on once
     you've got a few hundred closed trades; on tiny samples it will just
     overfit and add noise, not signal.

Nothing here ever *invents* a trade idea — it only reweights candidates
that the strategy modules already produced, and it never overrides
risk_manager's hard caps.
"""

import json
import logging
import sqlite3
from datetime import datetime

log = logging.getLogger("learning")

DEFAULTS = {
    "enabled": True,
    "min_trades_for_multiplier": 5,
    "multiplier_floor": 0.5,
    "multiplier_ceiling": 1.5,
    # Dollar scale that maps average realized P&L per trade onto the
    # multiplier. A strategy averaging +expectancy_scale_dollars per trade
    # gets the full expectancy-side boost; -expectancy_scale_dollars gets
    # the full penalty. Tune to roughly one "typical" win size for your risk
    # sizing. This exists specifically so a high win-rate/small-win,
    # occasional-large-loss strategy (the classic credit-spread failure
    # mode) doesn't get rewarded on win rate alone while its expectancy is
    # actually negative.
    "expectancy_scale_dollars": 100.0,
    "use_ml": False,
    "ml_min_training_rows": 150,
    "ml_retrain_every_n_trades": 25,
    "ml_model_path": "./learning_model.joblib",
    "ml_blend_weight": 0.3,   # how much the ML win-prob shifts the final score, 0-1
}


class LearningEngine:
    def __init__(self, cfg: dict, db):
        self.cfg = {**DEFAULTS, **cfg.get("learning", {})}
        self.db = db
        self.conn = db.conn
        self._ml_model = None
        self._ml_feature_cols = None
        if self.cfg["use_ml"]:
            self._try_load_ml_model()

    # ---------- rule-based multiplier ----------

    def _strategy_symbol_stats(self, strategy: str, symbol: str):
        rows = self.conn.execute(
            "SELECT outcome, realized_pnl FROM trades WHERE strategy=? AND symbol=? "
            "AND outcome IN ('WIN','LOSS','BREAKEVEN')",
            (strategy, symbol),
        ).fetchall()
        return rows

    def _strategy_stats(self, strategy: str):
        rows = self.conn.execute(
            "SELECT outcome, realized_pnl FROM trades WHERE strategy=? "
            "AND outcome IN ('WIN','LOSS','BREAKEVEN')",
            (strategy,),
        ).fetchall()
        return rows

    def _multiplier_from_rows(self, rows, floor, ceiling):
        if not rows:
            return 1.0, 0
        wins = sum(1 for o, _ in rows if o == "WIN")
        win_rate = wins / len(rows)
        win_component = win_rate - 0.5  # -0.5..+0.5, 0 at a 50% win rate

        pnls = [p for _, p in rows if p is not None]
        if pnls:
            avg_pnl = sum(pnls) / len(pnls)
            scale = self.cfg["expectancy_scale_dollars"] or 100.0
            expectancy_component = max(-0.5, min(0.5, avg_pnl / scale))
        else:
            expectancy_component = 0.0

        # Neutral at 1.0x. Win rate and expectancy each pull the multiplier
        # in their own direction — a high win rate with negative expectancy
        # (many small wins, rare big losses) now nets out closer to neutral
        # or below, instead of being boosted on win rate alone.
        raw = 1.0 + win_component * 0.6 + expectancy_component * 0.4
        return max(floor, min(ceiling, raw)), len(rows)

    def score_multiplier(self, strategy: str, symbol: str) -> float:
        if not self.cfg["enabled"]:
            return 1.0
        floor, ceiling = self.cfg["multiplier_floor"], self.cfg["multiplier_ceiling"]

        pair_rows = self._strategy_symbol_stats(strategy, symbol)
        if len(pair_rows) >= self.cfg["min_trades_for_multiplier"]:
            mult, _ = self._multiplier_from_rows(pair_rows, floor, ceiling)
            return mult

        # fall back to strategy-wide stats if this specific symbol is thin
        strat_rows = self._strategy_stats(strategy)
        if len(strat_rows) >= self.cfg["min_trades_for_multiplier"]:
            mult, _ = self._multiplier_from_rows(strat_rows, floor, ceiling)
            return mult

        return 1.0  # no opinion yet

    def apply(self, candidate: dict) -> dict:
        """Mutates and returns candidate with an adjusted score. Keeps the
        original score around for transparency in the Telegram alert."""
        base_score = candidate.get("score", 0.5)
        mult = self.score_multiplier(candidate.get("strategy", ""), candidate.get("symbol", ""))

        ml_prob = None
        if self.cfg["use_ml"] and self._ml_model is not None:
            ml_prob = self._predict_win_prob(candidate)

        adjusted = base_score * mult
        if ml_prob is not None:
            w = self.cfg["ml_blend_weight"]
            adjusted = (1 - w) * adjusted + w * ml_prob

        candidate["score_before_learning"] = round(base_score, 3)
        candidate["learning_multiplier"] = round(mult, 3)
        if ml_prob is not None:
            candidate["ml_win_probability"] = round(ml_prob, 3)
        candidate["score"] = round(max(0.0, min(1.0, adjusted)), 3)
        return candidate

    # ---------- optional ML layer ----------

    def _try_load_ml_model(self):
        try:
            import joblib
            self._ml_model, self._ml_feature_cols = joblib.load(self.cfg["ml_model_path"])
            log.info("Loaded learning ML model from %s", self.cfg["ml_model_path"])
        except FileNotFoundError:
            log.info("No ML model on disk yet at %s — will train once enough data exists.",
                      self.cfg["ml_model_path"])
        except Exception as e:
            log.warning("Could not load ML model (%s) — falling back to rule-based only.", e)

    def _training_rows(self, cutoff_date: str = None):
        query = ("SELECT features_json, outcome FROM trades WHERE outcome IN ('WIN','LOSS') "
                 "AND features_json IS NOT NULL")
        params = []
        if cutoff_date:
            # Strictly BEFORE cutoff — a trade that closed exactly on the
            # cutoff day is still "today" from the walk-forward vantage
            # point and shouldn't leak into a model trained to predict it.
            query += " AND closed_at IS NOT NULL AND closed_at < ?"
            params.append(cutoff_date)
        rows = self.conn.execute(query, params).fetchall()
        return rows

    def maybe_retrain_ml_model(self):
        """Call periodically (e.g. daily). No-op if use_ml is off or there
        isn't enough data. Trains a plain logistic regression on the
        candidate feature snapshot recorded at entry time -> win/loss."""
        if not self.cfg["use_ml"]:
            return None
        return self._retrain(self._training_rows(), persist=True, log_ctx="live")

    def maybe_retrain_historical(self, cutoff_date: str):
        """Backtest-only entry point (see AUDIT_FINDINGS.md #8): retrains
        strictly on trades whose closed_at is before `cutoff_date`
        (ISO date/datetime string), so a walk-forward backtest never
        trains on outcomes from its own future test window. `score` and
        `est_price` are exactly the kind of forward-looking fields that
        leak if you fit on the whole dataset and then "backtest" over
        that same dataset — this only ever sees what would actually have
        been known as of `cutoff_date`.

        Intended call pattern (wired into BacktestEngine.run()): call this
        once on the first trading day of each calendar month, passing that
        day as cutoff_date, BEFORE scanning that day for new candidates.
        Ignores use_ml/ml_min_training_rows scheduling gates that
        maybe_retrain_ml_model() applies for live use — the backtest
        caller decides the cadence. Does NOT persist to
        learning.ml_model_path (that file is for the live bot only); the
        resulting model is only kept in memory on this LearningEngine
        instance for the remainder of the backtest run.
        """
        rows = self._training_rows(cutoff_date=cutoff_date)
        return self._retrain(rows, persist=False, log_ctx=f"historical, cutoff={cutoff_date}")

    def _retrain(self, rows, persist: bool, log_ctx: str):
        if len(rows) < self.cfg["ml_min_training_rows"]:
            log.info("ML (%s): only %d labeled trades so far, need %d — skipping training.",
                      log_ctx, len(rows), self.cfg["ml_min_training_rows"])
            return None

        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import accuracy_score
            import numpy as np
            import joblib
        except ImportError:
            log.warning("scikit-learn/joblib not installed — add them to requirements.txt "
                        "to use learning.use_ml. Skipping.")
            return None

        X, y, feature_cols = self._vectorize(rows)
        if X is None or len(set(y)) < 2:
            log.info("ML (%s): not enough label diversity yet (need both wins and losses) — skipping.", log_ctx)
            return None

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        model = LogisticRegression(max_iter=1000)
        model.fit(X_train, y_train)
        acc = accuracy_score(y_test, model.predict(X_test)) if len(X_test) else None

        if persist:
            joblib.dump((model, feature_cols), self.cfg["ml_model_path"])
        self._ml_model, self._ml_feature_cols = model, feature_cols
        log.info("ML (%s): retrained on %d rows, holdout accuracy=%s", log_ctx, len(y), acc)
        return {"n_rows": len(y), "holdout_accuracy": acc}

    def _vectorize(self, rows):
        import numpy as np
        parsed = []
        for features_json, outcome in rows:
            try:
                f = json.loads(features_json)
            except (TypeError, json.JSONDecodeError):
                continue
            f["_label"] = 1 if outcome == "WIN" else 0
            parsed.append(f)
        if not parsed:
            return None, None, None

        numeric_cols = ["score", "dte", "est_price", "max_loss_per_contract"]
        strategies = sorted({p.get("strategy") for p in parsed if p.get("strategy")})
        feature_cols = numeric_cols + [f"strategy_{s}" for s in strategies]

        X, y = [], []
        for p in parsed:
            row = [float(p.get(c) or 0.0) for c in numeric_cols]
            row += [1.0 if p.get("strategy") == s else 0.0 for s in strategies]
            X.append(row)
            y.append(p["_label"])
        return np.array(X), np.array(y), feature_cols

    def _predict_win_prob(self, candidate: dict) -> float:
        import numpy as np
        f = {
            "score": candidate.get("score"),
            "dte": candidate.get("dte"),
            "est_price": candidate.get("est_price"),
            "max_loss_per_contract": candidate.get("max_loss_per_contract"),
            "strategy": candidate.get("strategy"),
        }
        row = []
        for col in self._ml_feature_cols:
            if col.startswith("strategy_"):
                row.append(1.0 if col == f"strategy_{f['strategy']}" else 0.0)
            else:
                row.append(float(f.get(col) or 0.0))
        proba = self._ml_model.predict_proba(np.array([row]))[0]
        classes = list(self._ml_model.classes_)
        return float(proba[classes.index(1)]) if 1 in classes else 0.5

    # ---------- reporting ----------

    def summarize(self) -> str:
        """Human-readable learnings summary for the periodic Telegram report."""
        lines = ["*Learning summary*"]
        strategies = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT strategy FROM trades WHERE strategy IS NOT NULL").fetchall()]
        if not strategies:
            return "No closed trades with recorded strategy yet — nothing to learn from."

        for strat in strategies:
            rows = self._strategy_stats(strat)
            if not rows:
                lines.append(f"• {strat}: no closed trades yet")
                continue
            wins = sum(1 for o, _ in rows if o == "WIN")
            pnls = [p for _, p in rows if p is not None]
            win_rate = wins / len(rows)
            total_pnl = sum(pnls) if pnls else None
            mult, _ = self._multiplier_from_rows(rows, self.cfg["multiplier_floor"], self.cfg["multiplier_ceiling"])
            pnl_str = f"${total_pnl:.2f}" if total_pnl is not None else "n/a"
            lines.append(f"• {strat}: {len(rows)} closed, {win_rate:.0%} win rate, "
                         f"realized P&L {pnl_str}, current multiplier {mult:.2f}x")

        if self.cfg["use_ml"]:
            status = "loaded" if self._ml_model is not None else "not enough data yet"
            lines.append(f"ML model: {status}")

        return "\n".join(lines)
