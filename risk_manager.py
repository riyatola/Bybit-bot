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
from datetime import date

log = logging.getLogger("risk_manager")

DEFAULTS = {
    "max_risk_per_trade_pct": 0.02,
    "max_concurrent_positions": 4,
    "max_positions_per_symbol": 1,
    "max_new_trades_per_day": 5,
}


class RiskManager:
    def __init__(self, cfg: dict, db, guardrails=None, market_data=None):
        r_cfg = cfg.get("risk", {}) or {}
        self.cfg = {**DEFAULTS, **r_cfg}
        self.db = db
        self.conn = db.conn
        self.guardrails = guardrails
        self.market_data = market_data

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
            # No account info yet (e.g. first run in paper mode): assume modest $100 risk cap
            max_risk_dollars = 100.0
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
        qty = max(qty, 1)
        # Optional hard cap from config
        qty_cap = self.cfg.get("max_contracts_per_trade")
        if qty_cap:
            qty = min(qty, int(qty_cap))

        candidate["suggested_qty"] = qty
        candidate["total_risk_at_suggested_qty"] = round(max_loss_per * qty, 2)
        candidate["max_risk_per_trade_pct"] = max_risk_pct
        return candidate

    # ---------- gating ----------

    def gate(self, candidate: dict, snapshot: dict) -> bool:
        """Runs hard limits. Returns True if the candidate is allowed to
        proceed to the approval queue, False if it should be dropped."""
        symbol = candidate.get("symbol", "").upper()
        strategy = candidate.get("strategy", "")

        # 1. Guardrails dynamic ban
        if self.guardrails and self.guardrails.is_banned(symbol, strategy):
            log.info("Gate: %s (%s) rejected by guardrails ban", symbol, strategy)
            return False

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
            log.info("Gate: %s (%s) rejected — already %d open (cap %d)",
                     symbol, strategy, open_count, max_concurrent)
            return False

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
                log.info("Gate: %s (%s) rejected — already %d open on %s (cap %d)",
                         symbol, strategy, n, symbol, max_per_symbol)
                return False

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
                log.info("Gate: %s (%s) rejected — %d total actions today (cap %d)",
                         symbol, strategy, total_today, day_cap)
                return False

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
                log.info("Gate: %s (%s) rejected — sector %s risk would exceed %0.f%% cap",
                         symbol, strategy, this_sector, cap * 100)
                return False

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
