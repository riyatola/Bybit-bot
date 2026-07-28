"""
RiskManager: sizes candidates post-discovery and applies hard gates before
they reach the approval queue. Implements two kinds of controls:

  1. size_candidate(candidate, net_liq) — converts the strategy's raw
     candidate (which has no suggested_qty) into a sized candidate:
        suggested_qty = floor(max_risk_per_trade_dollars / max_loss_per_contract)
     If the strategy didn't populate max_loss_per_contract (can happen for
     covered calls where loss is covered by spot), falls back to a
     cash-secured debit-based sizing.

  2. gate(candidate, snapshot) — True/False gating against:
        - max_concurrent_positions (total)
        - max_positions_per_symbol
        - max_new_trades_per_day (rolled from approval_manager's DB)
        - guardrails.is_banned() per (symbol, strategy)
        - whether the candidate would cause a per-symbol sector/industry
          concentration breach (uses open_position_risk_by_sector)

Called from scanner.py right before db.create_approval(...).
"""

import logging
from dataclasses import dataclass
from datetime import date
from math import copysign, isnan

log = logging.getLogger("risk_manager")

DEFAULTS = {
    "max_risk_per_trade_pct": 0.02,
    "max_concurrent_positions": 4,
    "max_positions_per_symbol": 1,
    "max_new_trades_per_day": 5,
    "min_contracts_per_trade": 1,
    "max_contracts_per_trade": 10,
    "min_credit_dollars": 5.0,
    "max_credit_dollars": 5000.0,
    "min_debit_dollars": 10.0,
    "max_debit_dollars": 5000.0,
    "max_total_risk_dollars": 5000.0,
    # Portfolio-wide greeks-based hard limits. Each is measured in USD notional/impact.
    # Example ($10,000 starting net_liq defaults scaled down from the €100k / 50k/2k/500 guidance.
    # Users should scale these UP for larger AUMs.
    "portfolio_greeks": {
        "max_delta_usd": 500,      # 5% of $10k = $500 net delta (perp-equivalent)
        "max_vega_usd": 200,       # $200 P&L per 1 vol-point IV move
        "max_gamma_usd": 50,        # $50 acceleration (1% spot move changes delta by $50)
        "max_theta_usd": 200,       # Max $200 daily decay income / single new trade can add
    },
}


@dataclass
class PortfolioGreeks:
    """Net USD-equivalent greeks across all open positions.

    Definitions (one contract = 1 unit of underlying for this bot's sizing):
      delta_usd:   delta * spot * contracts   — perp-equivalent net delta exposure
      vega_usd:    vega  * contracts * 100 — 1 IV-point = 1% change
      gamma_usd:   gamma * spot² * contracts * 100 — 1% spot move changes delta by this much
      theta_usd:   theta * contracts * 1   — daily decay (income for short options)
    """
    delta_usd: float = 0.0
    vega_usd: float = 0.0
    gamma_usd: float = 0.0
    theta_usd: float = 0.0

    def __add__(self, other: "PortfolioGreeks") -> "PortfolioGreeks":
        return PortfolioGreeks(
            delta_usd=self.delta_usd + other.delta_usd,
            vega_usd=self.vega_usd + other.vega_usd,
            gamma_usd=self.gamma_usd + other.gamma_usd,
            theta_usd=self.theta_usd + other.theta_usd,
        )

    def scale(self, factor: float) -> "PortfolioGreeks":
        return PortfolioGreeks(
            delta_usd=self.delta_usd * factor,
            vega_usd=self.vega_usd * factor,
            gamma_usd=self.gamma_usd * factor,
            theta_usd=self.theta_usd * factor,
        )

    def _safe_abs(self, x: float) -> float:
        if x is None or isnan(x):
            return 0.0
        return abs(x)

    def fits_inside(self, limits: dict) -> bool:
        """Return True if this greeks fit inside the provided portfolio limits dict
        (max_delta_usd/max_vega_usd/max_gamma_usd/max_theta_usd)."""
        if self._safe_abs(self.delta_usd) > float(limits.get("max_delta_usd") or 0):
            return False
        if self._safe_abs(self.vega_usd) > float(limits.get("max_vega_usd") or 0):
            return False
        if self._safe_abs(self.gamma_usd) > float(limits.get("max_gamma_usd") or 0):
            return False
        if self._safe_abs(self.theta_usd) > float(limits.get("max_theta_usd") or 0):
            return False
        return True

    def headroom(self, limits: dict) -> dict:
        """How much additional greeks *remaining* headroom we have vs. limits (positive = good)."""
        room_delta = float(limits.get("max_delta_usd") or 0) - self._safe_abs(self.delta_usd)
        room_vega = float(limits.get("max_vega_usd") or 0) - self._safe_abs(self.vega_usd)
        room_gamma = float(limits.get("max_gamma_usd") or 0) - self._safe_abs(self.gamma_usd)
        room_theta = float(limits.get("max_theta_usd") or 0) - self._safe_abs(self.theta_usd)
        return {"delta": room_delta, "vega": room_vega, "gamma": room_gamma, "theta": room_theta}


class RiskManager:
    def __init__(self, cfg: dict, db, guardrails=None, market_data=None):
        r_cfg = cfg.get("risk", {}) or {}
        self.cfg = {**DEFAULTS, **r_cfg}
        self.db = db
        self.conn = db.conn
        self.guardrails = guardrails
        self.market_data = market_data
        # Per-symbol spot cache for this scan cycle.
        # Getting spot is cheap (cached by market_data internally) but we
        # dedupe here so 20 DOT rejections don't each cause a separate lookup.
        self._spot_cache: dict[str, float | None] = {}
        # First-rejection log dedupe: (symbol, strategy, gate_name) set.
        # Reset via reset_scan_caches() at the top of each scan cycle
        # (or grows to modest worst-case ~320 entries).
        self._reject_log_seen: set[tuple[str, str, str]] = set()
        # Greeks-cap configuration (merged from DEFAULTS + cfg override)
        greeks_cfg = {}
        if isinstance(self.cfg.get("portfolio_greeks"), dict):
            greeks_cfg = {**DEFAULTS["portfolio_greeks"], **self.cfg["portfolio_greeks"]}
        else:
            greeks_cfg = dict(DEFAULTS["portfolio_greeks"])
        self.greeks_limits: dict[str, float] = {
            k: float(v) for k, v in greeks_cfg.items() if isinstance(v, (int, float))
        }
        log.info("Portfolio greeks limits: %s", self.greeks_limits)

    # ---------- helpers ----------

    def _get_spot(self, symbol: str) -> float | None:
        s = (symbol or "").upper()
        if not s:
            return None
        if s in self._spot_cache:
            return self._spot_cache[s]
        spot: float | None = None
        if self.market_data is not None:
            try:
                spot = self.market_data.get_last_price(s)
            except Exception as e:
                log.debug("spot lookup failed for %s: %s", s, e)
                spot = None
        # Sanity: clip tiny spot values to avoid infinite % premium floors
        if spot is not None and spot < 0.01:
            spot = 0.01
        self._spot_cache[s] = spot
        return spot

    def _thresholds_for_symbol(self, symbol: str) -> tuple[float, float]:
        """Return (min_credit_dollars, min_debit_dollars) adjusted per symbol.

        The configured absolute floors ($5 credit / $10 debit) make sense for
        high-priced underlyings (BTC, ETH), but they physically cannot be met
        on sub-$10 tokens (DOT, ADA, XRP, etc.) because a single option
        contract controls only 1 unit of the underlying.

        Two-tier rule (pick the smaller threshold for each symbol):
            min_credit = min( absolute_cfg_credit , spot * 0.5% )
            min_debit  = min( absolute_cfg_debit  , spot * 1.0% )

        Example results:
            BTC $60k : min_credit min($5, $300)  = $5    (cfg kept)
            ETH $3k  : min_credit min($5, $15)   = $5    (cfg kept)
            SOL $150 : min_credit min($5, $0.75) = $0.75 (scaled)
            DOT $7   : min_credit min($5, $0.035)= $0.035(scaled)
            ADA $0.4 : min_credit min($5, $0.002)= $0.002(scaled)
        """
        abs_min_credit = float(self.cfg.get("min_credit_dollars") or 0)
        abs_min_debit = float(self.cfg.get("min_debit_dollars") or 0)
        spot = self._get_spot(symbol)
        if spot is None or spot <= 0:
            return abs_min_credit, abs_min_debit
        pct_min_credit = spot * 0.005   # 0.5% of spot
        pct_min_debit = spot * 0.010    # 1.0% of spot
        min_credit = min(abs_min_credit, pct_min_credit) if abs_min_credit > 0 else pct_min_credit
        min_debit = min(abs_min_debit, pct_min_debit) if abs_min_debit > 0 else pct_min_debit
        # Floors to avoid 0.0001-type thresholds (still below penny, just avoid div-by-0 later)
        MIN_EPSILON = 0.0001
        return max(min_credit, MIN_EPSILON), max(min_debit, MIN_EPSILON)

    def reset_scan_caches(self):
        """Called at the start of each scan cycle so per-cycle caches and
        first-rejection log dedupe don't carry across scan boundaries."""
        self._spot_cache.clear()
        self._reject_log_seen.clear()

    # ---------- portfolio greeks engine ----------

    def _per_contract_greeks(self, candidate: dict) -> PortfolioGreeks:
        """Return the per-1-contract USD-equivalent greeks of a candidate.
        Strategies populate `candidate['legs'] = [{delta, gamma, vega, theta, quantity(ratio)} ...]`.
        If missing any field, falls back to heuristic based on strategy class."""
        import json
        symbol = (candidate.get("symbol") or "").upper()
        spot = self._get_spot(symbol) or 100.0
        legs = candidate.get("legs") or []
        delta = gamma = vega = theta = 0.0
        if legs:
            for leg in legs:
                ratio = int(leg.get("quantity") or leg.get("contracts") or 1)
                # Long/short sign: SELL instruction reverses the greeks sign
                instruction = str(leg.get("instruction") or leg.get("action") or "BUY").upper()
                sign = -1.0 if "SELL" in instruction or "SHORT" in instruction else +1.0
                d = float(leg.get("delta") or 0)
                g = float(leg.get("gamma") or 0)
                v = float(leg.get("vega") or 0)
                t = float(leg.get("theta") or 0)
                delta += sign * d * ratio
                gamma += sign * g * ratio
                vega += sign * v * ratio
                theta += sign * t * ratio
        else:
            # Heuristic fallback if no legs populated: short options (credit strategies)
            # have negative theta decay income, vega short = short vol play; long options reverse.
            strat = (candidate.get("strategy") or "").lower()
            is_credit = bool(candidate.get("is_credit"))
            if is_credit or strat in ("credit_spreads", "wheel", "covered_call"):
                # Short premium: short delta (put spread ~ +0.15-0.3 delta, credit call spread ~ -0.15)
                put_spread = "put" in strat or (strat == "wheel")
                d = +0.2 if put_spread else -0.2
                delta = d
                gamma = -0.001 * spot / 1000.0
                vega = -0.10 * spot / 1000.0
                theta = +0.01 * spot / 100.0  # income for short premium
            else:
                delta = +0.5 if "call" in strat or strat == "directional" else -0.5
                gamma = 0.001 * spot / 1000.0
                vega = 0.10 * spot / 1000.0
                theta = -0.01 * spot / 100.0

        # Convert raw per-contract greeks to USD-equivalent using spot:
        #   delta_usd   = delta * spot
        #   vega_usd    = vega * 100 (1 IV point = 1%)
        #   gamma_usd   = gamma * spot^2 * 100 (1% spot move changes delta by this $)
        #   theta_usd   = theta * 1 (daily)
        delta_usd = delta * spot
        vega_usd = vega * 100.0
        gamma_usd = gamma * (spot ** 2) * 100.0
        theta_usd = theta * 1.0
        return PortfolioGreeks(delta_usd=delta_usd, vega_usd=vega_usd, gamma_usd=gamma_usd, theta_usd=theta_usd)

    def open_portfolio_greeks(self) -> PortfolioGreeks:
        """Aggregate PortfolioGreeks across all EXECUTED/OPEN trades in DB.
        Best-effort: reads greeks per contract from entry_greeks_json (which we
        save at approval time) or falls back to heuristic from candidate
        strategy + is_credit. Always returns a valid object (zeroed if no open
        positions or no greeks metadata stored)."""
        import json
        total = PortfolioGreeks()
        rows = self.conn.execute(
            "SELECT id, symbol, strategy, is_credit, entry_greeks_json, features_json "
            "FROM trades WHERE status='EXECUTED' AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()
        for r in rows:
            trade_id, sym, strat, is_credit, greeks_json, feats_json = r
            per_contract: PortfolioGreeks | None = None
            contracts = 1
            try:
                if greeks_json:
                    g = json.loads(greeks_json)
                    per_contract = PortfolioGreeks(
                        delta_usd=float(g.get("delta_usd") or 0),
                        vega_usd=float(g.get("vega_usd") or 0),
                        gamma_usd=float(g.get("gamma_usd") or 0),
                        theta_usd=float(g.get("theta_usd") or 0),
                    )
                    contracts = int(g.get("contracts") or 1)
            except Exception as e:
                log.debug("Failed to parse entry_greeks_json for trade %s: %s", trade_id, e)
                per_contract = None
            if per_contract is None:
                # Heuristic fallback: build a fake candidate + use _per_contract_greeks
                fake = {"symbol": sym, "strategy": strat, "is_credit": bool(is_credit), "legs": []}
                per_contract = self._per_contract_greeks(fake)
            total = total + per_contract.scale(float(contracts))
        return total

    def _greeks_qty_cap(self, candidate: dict) -> int:
        """Max number of contracts this candidate can add without breaching
        the portfolio greeks limits. Returns 0 if even 1 contract would breach
        (caller should treat as gate failure). Returns 1e9 if greeks limits
        are all-zero / disabled (so the other sizing rules dominate)."""
        limits = self.greeks_limits
        if all(v <= 0 for v in limits.values()):
            return 1_000_000_000
        per_contract = self._per_contract_greeks(candidate)
        current = self.open_portfolio_greeks()

        # For each greek: how many contracts can we add before the limit is hit?
        # If even one contract on any dimension is already over the limit, return 0.
        def _room_for_one(current_abs: float, per_abs: float, limit: float) -> int:
            if limit <= 0:
                return 1_000_000_000
            if current_abs + per_abs <= limit:
                return 1_000_000_000
            remaining = max(0.0, limit - current_abs)
            if per_abs <= 0 or remaining <= 0:
                return 0
            return max(0, int(remaining // per_abs))

        caps = []
        for greek_name in ("delta", "vega", "gamma", "theta"):
            lim_key = f"max_{greek_name}_usd"
            lim = float(limits.get(lim_key) or 0)
            cur_g = getattr(current, f"{greek_name}_usd")
            per_g = getattr(per_contract, f"{greek_name}_usd")
            # Use signed values only for directional/theta (vega/gamma/delta use abs because
            # short vega and long vega are BOTH exposures that blow up in opposite tails).
            # Exception: theta — we care about net-income/decay magnitude.
            use_abs = greek_name in ("delta", "vega", "gamma", "theta")
            cur_a = abs(cur_g) if use_abs else cur_g
            per_a = abs(per_g) if use_abs else per_g
            cap = _room_for_one(cur_a, per_a, lim)
            caps.append(cap)
        return max(0, min(caps))

    # ---------- snapshot ----------

    def account_snapshot(self, client, account_hash: str) -> dict:
        """Pulls account details through the client and returns a flat dict
        used by both sizing and gating."""
        resp = client.account_details(account_hash, fields="positions")
        try:
            resp.raise_for_status()
        except Exception as e:
            log.warning("account_snapshot fetch failed: %s — using empty snapshot", e)
            return {
                "positions": [],
                "net_liq": 0.0,
                "cash": 0.0,
                "symbols_open": set(),
                "strategy_counts": {},
            }
        data = resp.json()["securitiesAccount"]
        positions = data.get("positions", [])
        net_liq = float(data.get("net_liq", 0) or 0)
        cash = float(data.get("cash_available", 0) or data.get("cash", 0) or 0)
        symbols_open = set()
        strategy_counts: dict[str, int] = {}
        for p in positions:
            instr = p.get("instrument", {})
            sym = instr.get("symbol", "")
            asset_type = instr.get("assetType", "")
            if asset_type == "OPTION":
                # Try to strip down to underlying from the symbol (parsing is best-effort)
                underlying = self._extract_underlying(sym)
                if underlying:
                    symbols_open.add(underlying.upper())
            elif asset_type == "CRYPTO" and sym:
                symbols_open.add(sym.upper())
            # Strategy counts: infer from the saved features_json via trade_tracker (approximate)
        # Better strategy counts source: pull from trades table (EXECUTED, outcome OPEN)
        rows = self.conn.execute(
            "SELECT symbol, strategy FROM trades "
            "WHERE status='EXECUTED' AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()
        for _, strat in rows:
            if strat:
                strategy_counts[strat] = strategy_counts.get(strat, 0) + 1
        symbols_open_from_db = {s for s, _ in rows if s}
        symbols_open |= symbols_open_from_db

        return {
            "positions": positions,
            "net_liq": net_liq,
            "cash": cash,
            "symbols_open": symbols_open,
            "strategy_counts": strategy_counts,
        }

    # ---------- sizing ----------

    def size_candidate(self, candidate: dict, net_liq: float) -> dict:
        """Fills suggested_qty + total_risk_at_suggested_qty in-place and
        returns the same candidate. Net-liq aware sizing."""
        max_risk_pct = self.cfg["max_risk_per_trade_pct"] or 0
        if net_liq <= 0:
            # No account info yet (e.g. first run in paper mode): assume modest $200 risk cap
            max_risk_dollars = 200.0
        else:
            max_risk_dollars = net_liq * max_risk_pct

        max_loss_per = float(candidate.get("max_loss_per_contract") or 0)
        if max_loss_per <= 0:
            # Strategy didn't set max_loss. Fall back: debit * 1.0 = risk.
            est_price = float(candidate.get("est_price") or 0)
            if candidate.get("is_credit"):
                # For credit trades without a defined max_loss (rare): cap at
                # 2x the credit received (pessimistic guess), but always >=1 so floor.
                max_loss_per = max(est_price * 2.0, est_price + 1.0)
            else:
                max_loss_per = max(est_price, 1.0)

        if max_loss_per <= 0:
            qty = 1
        else:
            qty = max(1, int(max_risk_dollars // max_loss_per))

        # Floor: always at least 1 contract so the human sees a concrete suggestion
        min_qty = int(self.cfg.get("min_contracts_per_trade") or 1)
        qty = max(qty, min_qty, 1)
        # Optional hard cap from config
        qty_cap = self.cfg.get("max_contracts_per_trade")
        if qty_cap:
            qty = min(qty, int(qty_cap))

        candidate["suggested_qty"] = qty
        total_risk = round(max_loss_per * qty, 2)
        candidate["total_risk_at_suggested_qty"] = total_risk
        candidate["max_risk_per_trade_pct"] = max_risk_pct

        # Hard dollar cap: if total risk still exceeds max_total_risk_dollars,
        # shrink qty until it fits (but never below min_qty).
        max_total_risk = float(self.cfg.get("max_total_risk_dollars") or 0)
        if max_total_risk > 0 and total_risk > max_total_risk:
            max_qty_fits = max(min_qty, int(max_total_risk // max_loss_per)) if max_loss_per > 0 else min_qty
            clamped = max(min_qty, min(qty, max_qty_fits))
            candidate["suggested_qty"] = clamped
            candidate["total_risk_at_suggested_qty"] = round(max_loss_per * clamped, 2)
            candidate["_sizing_reason"] = f"clamped to max_total_risk ${max_total_risk:.0f}"

        # GREEKS CAP (Priority 3): compute max contracts that fit within
        # portfolio-wide delta/vega/gamma/theta USD limits.
        greeks_cap = self._greeks_qty_cap(candidate)
        per_contract = self._per_contract_greeks(candidate)
        final_qty = candidate["suggested_qty"]
        if greeks_cap < final_qty:
            final_qty = max(min_qty, greeks_cap)  # shrink; respect min_qty
            if final_qty != candidate["suggested_qty"]:
                candidate["suggested_qty"] = final_qty
                candidate["total_risk_at_suggested_qty"] = round(max_loss_per * final_qty, 2)
                candidate["_sizing_reason"] = (
                    f"{candidate.get('_sizing_reason', '')} greeks-cap shrank qty to {final_qty}"
                ).strip()
        if greeks_cap <= 0 and final_qty > 0:
            # Even 1 contract would breach greeks limits — flag for gate (gate will drop it).
            candidate["_greeks_breach"] = True

        # Persist per-contract greeks for later portfolio aggregation.
        candidate["_entry_greeks"] = {
            "delta_usd": round(per_contract.delta_usd, 6),
            "vega_usd": round(per_contract.vega_usd, 6),
            "gamma_usd": round(per_contract.gamma_usd, 6),
            "theta_usd": round(per_contract.theta_usd, 6),
            "contracts": final_qty,
        }

        return candidate

    # ---------- gating ----------

    def gate(self, candidate: dict, snapshot: dict) -> bool:
        """Runs hard limits. Returns True if the candidate is allowed to
        proceed to the approval queue, False if it should be dropped.

        To avoid 20+ identical log lines per scan (e.g. 20 DOT strikes all
        failing the same debit floor), we remember per-(symbol, strategy,
        gate_name) the first rejection and log it only once per scan. A
        companion `reset_scan_caches()` is called at the start of each
        scan cycle to clear the dedupe set (or the cache just grows
        modestly — worst case 10 symbols × 4 strategies × 8 gates = 320
        entries, negligible)."""
        symbol = candidate.get("symbol", "").upper()
        strategy = candidate.get("strategy", "")

        def _reject(gate: str, msg: str) -> bool:
            key = (symbol, strategy, gate)
            if key not in self._reject_log_seen:
                self._reject_log_seen.add(key)
                log.info("Gate: %s (%s) rejected — %s", symbol, strategy, msg)
            return False

        # 0. Credit/Debit dollar gates (micro-premium or crazy-expensive candidates)
        #    Per-symbol adjusted: small tokens get much lower dollar floors.
        est_price = float(candidate.get("est_price") or 0)
        min_credit, min_debit = self._thresholds_for_symbol(symbol)
        max_credit = float(self.cfg.get("max_credit_dollars") or 0)
        max_debit = float(self.cfg.get("max_debit_dollars") or 0)
        if candidate.get("is_credit"):
            lo = min_credit
            hi = max_credit
            if lo > 0 and est_price < lo:
                return _reject(
                    "min_credit",
                    f"credit ${est_price:.4f} below min ${lo:.4f} (spot-adjusted)",
                )
            if hi > 0 and est_price > hi:
                return _reject(
                    "max_credit",
                    f"credit ${est_price:.2f} above max ${hi:.2f}",
                )
        else:
            lo = min_debit
            hi = max_debit
            if lo > 0 and est_price < lo:
                return _reject(
                    "min_debit",
                    f"debit ${est_price:.4f} below min ${lo:.4f} (spot-adjusted)",
                )
            if hi > 0 and est_price > hi:
                return _reject(
                    "max_debit",
                    f"debit ${est_price:.2f} above max ${hi:.2f}",
                )

        # 0b. Total-risk dollar cap (after sizing)
        total_risk = float(candidate.get("total_risk_at_suggested_qty") or 0)
        risk_cap = float(self.cfg.get("max_total_risk_dollars") or 0)
        if risk_cap > 0 and total_risk > risk_cap:
            return _reject(
                "max_total_risk",
                f"total risk ${total_risk:.2f} above cap ${risk_cap:.2f}",
            )

        # 0c. Portfolio Greeks hard cap (delta/vega/gamma/theta USD limits)
        if candidate.pop("_greeks_breach", False):
            # If we got here, even 1 contract breaches at least one limit.
            current = self.open_portfolio_greeks()
            room = current.headroom(self.greeks_limits)
            per = self._per_contract_greeks(candidate)
            worst = max(
                ("delta", room["delta"], abs(per.delta_usd)),
                ("vega",  room["vega"],  abs(per.vega_usd)),
                ("gamma", room["gamma"], abs(per.gamma_usd)),
                ("theta", room["theta"], abs(per.theta_usd)),
                key=lambda t: (t[1] - t[2]),
            )
            return _reject(
                "portfolio_greeks_cap",
                f"even 1 contract would breach {worst[0]} cap (remaining=${worst[1]:.2f}, need=${worst[2]:.2f})",
            )

        # 1. Guardrails dynamic ban
        if self.guardrails and self.guardrails.is_banned(symbol, strategy):
            return _reject("guardrails_ban", "banned by guardrails")

        # 2. Max concurrent positions (open positions total)
        max_concurrent = self.cfg.get("max_concurrent_positions")
        open_count = 0
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='EXECUTED' "
            "AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchone()
        if rows:
            open_count = rows[0] or 0
        if max_concurrent and open_count >= max_concurrent:
            return _reject(
                "max_concurrent",
                f"already {open_count} open (cap {max_concurrent})",
            )

        # 3. Per-symbol cap
        max_per_symbol = self.cfg.get("max_positions_per_symbol")
        if max_per_symbol:
            same_sym = self.conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='EXECUTED' "
                "AND (outcome='OPEN' OR outcome IS NULL) AND symbol=?",
                (symbol,),
            ).fetchone()
            n = same_sym[0] if same_sym else 0
            if n >= max_per_symbol:
                return _reject(
                    "max_per_symbol",
                    f"already {n} open on {symbol} (cap {max_per_symbol})",
                )

        # 4. New trades/day cap (counts PENDING/APPROVED/EXECUTED created today)
        day_cap = self.cfg.get("max_new_trades_per_day")
        if day_cap:
            today_iso = date.today().isoformat()
            approvals_today = self.db.count_trades_today(today_iso)
            pending_rows = self.conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE status='PENDING' AND created_at LIKE ?",
                (f"{today_iso}%",),
            ).fetchone()
            pending_n = pending_rows[0] if pending_rows else 0
            total_today = approvals_today + pending_n
            if total_today >= day_cap:
                return _reject(
                    "max_new_trades_today",
                    f"{total_today} total actions today (cap {day_cap})",
                )

        # 5. Sector concentration (optional, only if sector_map is configured)
        sector_cfg = self.cfg.get("sector_concentration")
        if sector_cfg:
            sector_map = sector_cfg.get("map", {}) or {}
            cap = sector_cfg.get("max_pct", 0.3)
            net_liq = snapshot.get("net_liq", 0) or 1
            risk_by_sector = self.db.open_position_risk_by_sector(sector_map)
            this_sector = sector_map.get(symbol, "Unknown")
            current_sector_risk = risk_by_sector.get(this_sector, 0.0)
            adding_risk = float(candidate.get("total_risk_at_suggested_qty") or 0)
            if net_liq > 0 and ((current_sector_risk + adding_risk) / net_liq) > cap:
                return _reject(
                    "sector_concentration",
                    f"sector {this_sector} risk would exceed {cap*100:.0f}% cap",
                )

        return True

    # ---------- helpers ----------

    @staticmethod
    def _extract_underlying(bybit_option_symbol: str) -> str:
        """Best-effort: option symbols like BTC-26AUG24-65000-C -> BTC."""
        if not bybit_option_symbol:
            return ""
        for sep in ("-", "_"):
            if sep in bybit_option_symbol:
                head = bybit_option_symbol.split(sep)[0]
                if head and head.isalpha():
                    return head.upper()
        return ""
