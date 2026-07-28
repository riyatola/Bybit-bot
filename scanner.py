"""
Main entrypoint for crypto options bot (Bybit).
Wires together: auth, market data, strategies, risk, approval, notifier, learning, guardrails, bybit_alpha, sentiment, circuit breaker.
"""

import asyncio
import argparse
import logging
import os
import sys
import yaml
from datetime import date, datetime, timedelta

from bybit_client import BybitClient, _format_api_error
from market_data import MarketDataAdapter
from paper_broker import PaperBrokerClient
from risk_manager import RiskManager
from approval_manager import Db
from notifier import TelegramNotifier
from executor import Executor
from strategies import credit_spreads, directional, earnings_vol, wheel
from strategies import (
    vrp_strangle, calendar_spread, risk_reversal,
    event_straddle, funding_carry, broken_wing_butterfly,
)
from guardrails import Guardrails
from learning import LearningEngine
from sentiment import SentimentEngine
from bybit_alpha import BybitAlphaEngine
from circuit_breaker import CircuitBreaker
from exit_manager import ExitManager
from trade_tracker import TradeTracker
from hedge_manager import DeltaHedger
from health_server import start_health_server_async, mark_event, get_port


def get_data_dir() -> str:
    base = os.environ.get("BOT_DATA_DIR", ".")
    os.makedirs(base, exist_ok=True)
    return base


def _configure_logging():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log_path = os.path.join(get_data_dir(), "bot.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("scanner")


def _as_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _overlay_env_config(cfg: dict) -> dict:
    """Apply environment-variable overrides for sensitive keys.

    Azure App Service / Container Apps expose Application Settings as env vars.
    This keeps secrets out of the Docker image and config.yaml in version control.
    """
    cfg = dict(cfg)

    # Top-level mode
    if os.environ.get("BOT_MODE"):
        cfg["mode"] = os.environ["BOT_MODE"]

    # Bybit credentials + testnet flag
    bybit = dict(cfg.get("bybit", {}) or {})
    if os.environ.get("BYBIT_API_KEY"):
        bybit["api_key"] = os.environ["BYBIT_API_KEY"]
    if os.environ.get("BYBIT_API_SECRET"):
        bybit["api_secret"] = os.environ["BYBIT_API_SECRET"]
    if "BYBIT_TESTNET" in os.environ:
        bybit["testnet"] = _as_bool(os.environ["BYBIT_TESTNET"], bybit.get("testnet", True))
    cfg["bybit"] = bybit

    # Telegram
    tg = dict(cfg.get("telegram", {}) or {})
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    cfg["telegram"] = tg

    # Learning: resolve ml_model_path relative to BOT_DATA_DIR if it's a relative path
    learning_cfg = dict(cfg.get("learning", {}) or {})
    model_path = learning_cfg.get("ml_model_path", "./learning_model.joblib")
    if model_path and not os.path.isabs(model_path):
        learning_cfg["ml_model_path"] = os.path.join(get_data_dir(), model_path)
    cfg["learning"] = learning_cfg

    # Risk / circuit breaker numeric overrides (optional)
    risk = dict(cfg.get("risk", {}) or {})
    for env_key, cfg_key in (
        ("RISK_MAX_PCT", "max_risk_per_trade_pct"),
        ("RISK_MAX_CONCURRENT", "max_concurrent_positions"),
        ("RISK_MAX_PER_SYMBOL", "max_positions_per_symbol"),
        ("RISK_MAX_NEW_PER_DAY", "max_new_trades_per_day"),
    ):
        if os.environ.get(env_key):
            try:
                risk[cfg_key] = float(os.environ[env_key]) if "." in os.environ[env_key] else int(os.environ[env_key])
            except ValueError:
                pass
    cfg["risk"] = risk

    cb = dict(cfg.get("circuit_breaker", {}) or {})
    if os.environ.get("CIRCUIT_DAILY_LOSS_DOLLARS"):
        try:
            cb["daily_loss_limit_dollars"] = float(os.environ["CIRCUIT_DAILY_LOSS_DOLLARS"])
        except ValueError:
            pass
    if "CIRCUIT_MANUAL_KILL" in os.environ:
        cb["manual_kill"] = _as_bool(os.environ["CIRCUIT_MANUAL_KILL"], False)
    cfg["circuit_breaker"] = cb

    return cfg


def load_config(path="config.yaml"):
    env_path = os.environ.get("CONFIG_PATH") or path
    if not os.path.isabs(env_path):
        env_path = os.path.join(get_data_dir(), env_path)
    # If config file doesn't exist (e.g. user relies fully on env vars),
    # use a minimal skeleton so env overlay still works.
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        log.warning("Config file not found at %s — falling back to env-var-only config.", env_path)
        cfg = {
            "mode": "paper",
            "bybit": {"api_key": "", "api_secret": "", "testnet": True},
            "paper": {"starting_cash": 10000.0, "slippage_bps": 5, "commission_per_contract": 0.0},
            "universe": {"symbols": ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "MATIC", "DOT"], "wheel_watchlist": ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "MATIC", "DOT"], "min_avg_option_volume": 0},
            "scan": {"poll_interval_seconds": 300},
            "strategies": {
                "credit_spreads": {"enabled": True, "min_iv_rank": 50, "target_short_delta": 0.20,
                    "min_credit_to_width_ratio": 0.25, "dte_min": 7, "dte_max": 30, "max_leg_spread_pct": 0.20},
                "directional": {"enabled": True, "trend_lookback_days": 20, "volume_multiple_trigger": 1.5,
                    "min_iv_rank": 20, "max_iv_rank": 60, "dte_min": 10, "dte_max": 30, "max_leg_spread_pct": 0.20},
                "earnings_vol": {"enabled": False},
                "wheel": {"enabled": True, "csp_target_delta": 0.25, "covered_call_target_delta": 0.25,
                    "dte_min": 7, "dte_max": 30, "max_leg_spread_pct": 0.20},
            },
            "risk": {"max_risk_per_trade_pct": 0.02, "max_concurrent_positions": 4,
                "max_positions_per_symbol": 1, "max_new_trades_per_day": 5},
            "approval": {"timeout_minutes": 15},
            "telegram": {"bot_token": "", "chat_id": ""},
            "learning": {"enabled": True, "min_trades_for_multiplier": 5, "multiplier_floor": 0.5,
                "multiplier_ceiling": 1.5, "use_ml": False, "ml_min_training_rows": 150,
                "ml_retrain_every_n_trades": 25, "ml_model_path": "./learning_model.joblib",
                "ml_blend_weight": 0.3, "outcome_poll_minutes": 45,
                "report_day_of_week": "sun", "report_hour": 18},
            "guardrails": {"enabled": True, "min_trades_for_stats": 5, "max_consecutive_losses": 3,
                "min_win_rate": 0.30, "max_cumulative_loss_dollars": 500, "ban_duration_days": 90},
            "sentiment": {"enabled": False, "persistence_days": 3, "directional_vix_min": 15.0,
                "directional_vix_max": 25.0, "directional_vix_band_multiplier": 0.0},
            "bybit_alpha": {"enabled": True, "cache_ttl_seconds": 300, "persistence_readings": 3,
                "long_short": {"period": "1h", "extreme_long_ratio": 0.65, "extreme_short_ratio": 0.35},
                "funding": {"high_positive": 0.0003, "high_negative": -0.0003, "oi_rise_pct": 5.0},
                "options_skew": {"dte_min": 7, "dte_max": 45, "bearish_put_call_oi": 1.25, "bullish_put_call_oi": 0.80},
                "multipliers": {
                    "directional": {"aligned": 1.12, "crowded_fade": 0.55},
                    "credit_spreads": {"high_skew_iv": 1.08, "crisis_funding": 0.50},
                    "wheel": {"extreme_fear": 1.10, "extreme_greed": 0.85},
                },
            },
            "circuit_breaker": {"enabled": True, "daily_loss_limit_dollars": 1000,
                "manual_kill": False, "check_interval_seconds": 300},
            "exits": {"enabled": True,
                "default_stop_loss_pct": {"credit_spreads": 0.50, "directional": 0.50, "wheel": 0.50},
                "default_take_profit_pct": {"credit_spreads": 0.50, "directional": 1.00, "wheel": 0.50},
                "force_exit_dte": 7, "approval_timeout_minutes": 10},
        }
    return _overlay_env_config(cfg)


def get_universe(cfg):
    """Static list of underlyings to scan."""
    return cfg.get("universe", {}).get("symbols", ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "MATIC", "DOT"])


async def run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier,
                         guardrails, learning, bybit_alpha, sentiment, circuit_breaker,
                         executor=None):
    db.expire_stale()
    if hasattr(risk, "reset_scan_caches"):
        risk.reset_scan_caches()

    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping scan")
        mark_event("scan")
        return

    # Refresh macro regime (sentiment may be disabled)
    regime = sentiment.refresh_macro_regime() if sentiment.cfg.get("enabled", False) else "NEUTRAL"
    log.info("Macro regime: %s", regime)

    snapshot = risk.account_snapshot(client, account_hash)
    symbols = get_universe(cfg)

    if bybit_alpha.cfg.get("enabled", False):
        bybit_alpha.refresh_signals(symbols)
        log.info("Bybit alpha signals refreshed for %s", symbols)

    # Filter banned symbols
    active_bans = {b["symbol"] for b in guardrails.list_active_bans()}
    if active_bans:
        symbols = [s for s in symbols if s not in active_bans]
    log.info("Scanning %d symbols (%d banned)", len(symbols), len(active_bans))

    # Volume pre-filter (optional)
    min_avg_vol = cfg["universe"].get("min_avg_option_volume", 0)
    if min_avg_vol:
        liquid = []
        for s in symbols:
            try:
                if market_data.get_avg_volume(s) >= min_avg_vol:
                    liquid.append(s)
            except Exception:
                pass
        symbols = liquid
        log.info("Liquidity prefilter: %d/%d meet min_avg_option_volume=%s",
                 len(symbols), len(liquid), min_avg_vol)

    all_candidates = []
    s_cfg = cfg["strategies"]

    if s_cfg["credit_spreads"]["enabled"]:
        all_candidates += credit_spreads.scan(client, symbols, cfg, market_data)
    if s_cfg["directional"]["enabled"]:
        all_candidates += directional.scan(client, symbols, cfg, market_data)
    if s_cfg["earnings_vol"]["enabled"]:
        all_candidates += earnings_vol.scan(client, symbols, cfg, market_data)
    if s_cfg["wheel"]["enabled"]:
        all_candidates += wheel.scan(client, snapshot["positions"], cfg, market_data)

    # Elite strategy pack (A-F). Each module also checks its own
    # strategies.<name>.enabled flag internally, but we gate on cfg here
    # too so a missing config block doesn't crash a strategy that's
    # simply not configured yet.
    for name, mod, needs_positions in (
        ("vrp_strangle", vrp_strangle, False),
        ("calendar_spread", calendar_spread, False),
        ("risk_reversal", risk_reversal, False),
        ("event_straddle", event_straddle, False),
        ("funding_carry", funding_carry, False),
        ("broken_wing_butterfly", broken_wing_butterfly, False),
    ):
        strat_cfg = s_cfg.get(name, {})
        if not strat_cfg.get("enabled", False):
            continue
        try:
            all_candidates += mod.scan(client, symbols, cfg, market_data)
        except Exception:
            log.exception("Strategy %s scan failed — skipping this cycle", name)

    log.info("Found %d raw candidates", len(all_candidates))

    # Apply learning, Bybit alpha, and sentiment adjustments
    all_candidates = [learning.apply(c) for c in all_candidates]
    all_candidates = [bybit_alpha.apply(c) for c in all_candidates]
    all_candidates = [sentiment.apply(c) for c in all_candidates]
    all_candidates.sort(key=lambda c: c["score"], reverse=True)

    for candidate in all_candidates:
        if circuit_breaker.is_tripped():
            break
        candidate = risk.size_candidate(candidate, snapshot["net_liq"])
        if not risk.gate(candidate, snapshot):
            continue

        # FULL AUTO MODE (user requested) — every trade that passes gate/size
        # executes immediately. Telegram sends an audit notification AFTER
        # execute, never an approval card. ALLOW_AUTO_APPROVE safety env is
        # bypassed here because user explicitly opted into full automation.
        if executor is None:
            log.error("Executor not available — skipping auto-exec of %s %s",
                      candidate["symbol"], candidate["strategy"])
            continue

        approval_cfg = cfg.get("approval", {}) or {}
        approval_timeout = int(approval_cfg.get("timeout_minutes") or 15)
        # Create PENDING approval record first (for audit trail even in auto mode)
        approval_id = db.create_approval(candidate, approval_timeout)
        db.set_status(approval_id, "APPROVED")   # full-auto: skip Telegram button flow

        score = float(candidate.get("score") or 0)
        strat = candidate.get("strategy") or "?"
        sym = candidate["symbol"]
        log.info("Auto-executing %s (%s) score=%.2f (approval %s)", sym, strat, score, approval_id)
        try:
            order_id = await executor.execute(candidate, approval_id)
            db.set_status(approval_id, "EXECUTED")
            log.info("Auto-execute OK: %s (%s) score=%.2f → order_id=%s", sym, strat, score, order_id)
            # Telegram notification (success) — this is your audit-trail arm.
            # No button, just the trade receipt.
            qty = candidate.get("suggested_qty") or candidate.get("num_contracts") or 1
            credit_debit = "credit" if candidate.get("is_credit") else "debit"
            price = candidate.get("est_price") or 0
            risk = candidate.get("total_risk_at_suggested_qty") or 0
            msg = (
                f"✅ *TRADE EXECUTED*\n"
                f"Symbol: `{sym}`\n"
                f"Strategy: `{strat}`\n"
                f"Score: `{score:.2f}`\n"
                f"Contracts: `{qty}`\n"
                f"{credit_debit.capitalize()}: `${price:.4f}`\n"
                f"Risk at entry: `${risk:.2f}`\n"
                f"Order: `{order_id}`\n"
                f"Approval ID: `{approval_id}`"
            )
            if candidate.get("expiration"):
                msg += f"\nExpiration: `{candidate['expiration']}`"
            if candidate.get("legs"):
                for leg in candidate["legs"]:
                    msg += f"\n  • {leg.get('instruction','?')} {leg.get('option_symbol', leg.get('symbol','?'))}"
            await notifier.send_note(msg, parse_mode="Markdown")
        except Exception as e:
            log.exception("Auto-execute FAILED for approval %s (%s %s)", approval_id, sym, strat)
            db.set_status(approval_id, "FAILED")
            err_msg = f"⚠️ *TRADE FAILED*\n{sym} {strat} (score {score:.2f}): `{e}`"
            try:
                await notifier.send_note(err_msg, parse_mode="Markdown")
            except Exception:
                pass

    mark_event("scan")


async def run_exit_check(exit_manager, db, notifier, circuit_breaker, executor=None):
    try:
        if circuit_breaker.is_tripped():
            return
        triggers = exit_manager.check_triggers()
        for trigger in triggers:
            if circuit_breaker.is_tripped():
                break
            candidate = exit_manager.build_exit_candidate(trigger)
            timeout = exit_manager.exit_cfg.get("approval_timeout_minutes", 10)
            approval_id = db.create_approval(candidate, timeout)
            exit_manager.conn.execute(
                "UPDATE trades SET exit_approval_id=? WHERE id=?",
                (approval_id, trigger["trade_id"]),
            )
            exit_manager.conn.commit()

            trade_id = trigger["trade_id"]
            sym = candidate.get("symbol") or "?"
            reason = trigger.get("reason") or "?"
            strat = candidate.get("strategy") or "exit"

            if executor is None:
                log.error("Executor unavailable — cannot auto-exit trade %s (%s)", trade_id, reason)
                continue

            db.set_status(approval_id, "APPROVED")
            log.info("Auto-exiting trade %s (%s, %s), approval %s", trade_id, sym, reason, approval_id)
            try:
                order_id = await executor.execute(candidate, approval_id)
                db.set_status(approval_id, "EXECUTED")
                log.info("Exit OK: trade %s (%s) → order %s", trade_id, reason, order_id)
                price = candidate.get("est_price") or 0
                credit_debit = "credit" if candidate.get("is_credit") else "debit"
                msg = (
                    f"🔻 *POSITION EXITED*\n"
                    f"Trade ID: `{trade_id}`\n"
                    f"Symbol: `{sym}`\n"
                    f"Trigger: `{reason}`\n"
                    f"Strategy: `{strat}`\n"
                    f"{credit_debit.capitalize()}: `${price:.4f}`\n"
                    f"Exit Order: `{order_id}`"
                )
                await notifier.send_note(msg, parse_mode="Markdown")
            except Exception as e:
                log.exception("Exit FAILED for trade %s (approval %s)", trade_id, approval_id)
                db.set_status(approval_id, "FAILED")
                err = f"❌ *EXIT FAILED*\n{sym} trade `{trade_id}` reason `{reason}`: `{e}`"
                try:
                    await notifier.send_note(err, parse_mode="Markdown")
                except Exception:
                    pass
            continue  # skip the manual approval send (unreachable, belt+suspenders)

    finally:
        mark_event("exit_check")


async def run_learning_cycle(db, guardrails, learning, bybit_alpha, sentiment, notifier):
    newly_banned = guardrails.evaluate_all_symbols()
    learning.maybe_retrain_ml_model()
    summary = learning.summarize() + "\n\n" + bybit_alpha.summarize() + "\n\n" + sentiment.summarize()
    active_bans = guardrails.list_active_bans()
    await notifier.send_learning_report(summary, newly_banned, active_bans)


async def _run_forever_interval(name: str, seconds: int, coro_fn, *args,
                                 suppress_exceptions: bool = True,
                                 initial_delay: int = 0):
    """Native asyncio interval loop — no thread pools, no hidden schedulers.

    Args:
        name: Human-readable task name for logs.
        seconds: Interval between the END of one run and the START of the next.
        coro_fn: Async callable (not called yet).
        *args: Positional args forwarded to coro_fn().
        suppress_exceptions: If True (default), exceptions are logged and the
            loop continues on the next tick; if False, the task exits and
            propagates the exception (bot will crash loudly, which is useful
            for catching startup bugs before supervision).
        initial_delay: Sleep this many seconds BEFORE the first call (useful
            to stagger initial tasks so they don't thundering-herd at boot).
    """
    log.info("Task %s registered (every %ds, initial_delay=%ds)", name, seconds, initial_delay)
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        t0 = datetime.utcnow()
        try:
            result = coro_fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            log.info("Task %s cancelled — shutting down cleanly", name)
            return
        except Exception as e:
            log.exception("Task %s CRASHED: %s", name, e)
            if not suppress_exceptions:
                raise
        finally:
            dt_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
            mark_event(name)
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            log.info("Task %s sleep cancelled — shutting down cleanly", name)
            return


_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def _run_weekly_cron(name: str, day_of_week: str, hour: int,
                            coro_fn, *args, tz_offset_hours: int = 0):
    """Simple weekly cron using native asyncio only.
    Fires once per week at the given weekday/hour (with optional UTC offset
    applied so 18:00 'local sun' doesn't require a tz library)."""
    target_wd = _WEEKDAY.get((day_of_week or "sun").lower(), 6)
    log.info("Cron %s registered (weekday=%s hour=%d tz_offset=%+dh)",
             name, day_of_week, hour, tz_offset_hours)
    while True:
        now = datetime.utcnow() + timedelta(hours=tz_offset_hours)
        # Compute next firing time
        days_until = (target_wd - now.weekday()) % 7
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if days_until == 0 and now >= candidate:
            days_until = 7
        next_run = candidate + timedelta(days=days_until)
        sleep_s = max(1.0, (next_run - now).total_seconds())
        log.info("Cron %s next run in %.0fs (≈ %.1fh)", name, sleep_s, sleep_s / 3600)
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            log.info("Cron %s cancelled — shutting down cleanly", name)
            return
        # Run it
        try:
            result = coro_fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            log.info("Cron %s run cancelled", name)
            return
        except Exception as e:
            log.exception("Cron %s CRASHED during run: %s", name, e)


async def _run_daily_cron(name: str, hour: int, minute: int, fn, *args,
                           tz_offset_hours: int = 0):
    """Run a (sync) callable daily at HH:MM with optional UTC offset.
    Used for paper-mode expired-position settlement."""
    log.info("Daily cron %s registered (%02d:%02d, tz_offset=%+dh)",
             name, hour, minute, tz_offset_hours)
    while True:
        now = datetime.utcnow() + timedelta(hours=tz_offset_hours)
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        sleep_s = max(1.0, (nxt - now).total_seconds())
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            log.info("Daily cron %s cancelled", name)
            return
        try:
            fn(*args)
        except Exception as e:
            log.exception("Daily cron %s CRASHED: %s", name, e)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-only", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    bybit_cfg = cfg["bybit"]
    real_client = BybitClient(
        api_key=bybit_cfg["api_key"],
        api_secret=bybit_cfg["api_secret"],
        testnet=bybit_cfg.get("testnet", True)
    )

    if args.auth_only:
        try:
            real_client.account_info()
            mark_event("bybit_auth")
            log.info("Auth successful — Bybit API key accepted.")
        except Exception as e:
            log.error("Auth failed: %s", _format_api_error(e, testnet=bybit_cfg.get("testnet", True)))
            sys.exit(1)
        return

    asyncio.create_task(start_health_server_async())
    log.info("Health server will bind to 0.0.0.0:%d (PORT=%s)",
             get_port(), os.environ.get("PORT"))

    # Paper wrapper if mode is paper
    if cfg.get("mode", "paper") == "paper":
        client = PaperBrokerClient(real_client, cfg)
        account_hash = "PAPER0"  # paper broker uses fixed hash
    else:
        client = real_client
        # For real, we need an account hash – Bybit uses account ID? We'll use a dummy for now.
        account_hash = "REAL_ACCOUNT"  # placeholder; adapt as needed

    try:
        real_client.account_info()
        mark_event("bybit_auth")
        log.info("Bybit auth OK (mode=%s, testnet=%s)",
                 cfg.get("mode", "paper"), bybit_cfg.get("testnet", True))
    except Exception as e:
        log.warning("Bybit auth probe failed on startup (continuing anyway): %s",
                    _format_api_error(e, testnet=bybit_cfg.get("testnet", True)))

    db_path = os.path.join(get_data_dir(), "bot.db")
    db = Db(db_path)
    market_data = MarketDataAdapter(client, cfg=cfg)
    guardrails = Guardrails(cfg, db)
    learning = LearningEngine(cfg, db)
    bybit_alpha = BybitAlphaEngine(cfg, db, client, market_data)
    sentiment = SentimentEngine(cfg, db, market_data=market_data)  # can be disabled in config
    risk = RiskManager(cfg, db, guardrails=guardrails, market_data=market_data)
    circuit_breaker = CircuitBreaker(cfg, db, client, account_hash, paper_mode=(cfg.get("mode")=="paper"))
    exit_manager = ExitManager(cfg, db, client, account_hash, market_data=market_data, sentiment=sentiment)
    trade_tracker = TradeTracker(db, client, account_hash)
    executor = Executor(cfg, client, account_hash, db, trade_tracker=trade_tracker)
    notifier = TelegramNotifier(cfg, db, executor)
    hedger = DeltaHedger(cfg, db, client, account_hash, market_data)

    await notifier.run_polling_forever()
    mark_event("telegram")
    await notifier.send_note("Crypto options scanner started.")
    log.info("Bot online. Health endpoint: http://0.0.0.0:%d/healthz", get_port())

    poll_s = int(cfg["scan"]["poll_interval_seconds"])
    outcome_poll_min = int(cfg.get("learning", {}).get("outcome_poll_minutes", 45))
    learning_wd = cfg.get("learning", {}).get("report_day_of_week", "sun")
    learning_hour = int(cfg.get("learning", {}).get("report_hour", 18))

    tasks = []

    # Primary scan cycle (fire once immediately, then every poll_s after completion)
    tasks.append(asyncio.create_task(_run_forever_interval(
        "scan", poll_s, run_scan_cycle,
        client, account_hash, cfg, market_data, risk, db, notifier,
        guardrails, learning, bybit_alpha, sentiment, circuit_breaker, executor,
    )))

    # Exit checks (same cadence as scan)
    tasks.append(asyncio.create_task(_run_forever_interval(
        "exit_check", poll_s, run_exit_check,
        exit_manager, db, notifier, circuit_breaker, executor,
        initial_delay=int(poll_s * 0.5),  # stagger 50% offset from scan
    )))

    # Trade outcome polling (every outcome_poll_min minutes)
    tasks.append(asyncio.create_task(_run_forever_interval(
        "outcome_poll", outcome_poll_min * 60, trade_tracker.poll_and_update_outcomes,
    )))

    # Delta hedging for the unhedged vol-selling/carry strategies (A/B/C/D/E)
    hedge_interval_s = int(cfg.get("hedge_manager", {}).get("rehedge_interval_seconds", 4 * 3600))
    if cfg.get("hedge_manager", {}).get("enabled", True):
        tasks.append(asyncio.create_task(_run_forever_interval(
            "delta_hedge", hedge_interval_s, hedger.rehedge_all,
            initial_delay=int(poll_s * 0.75),  # let the first scan cycle land first
        )))

    # Weekly learning report + guardrails evaluation + ML retrain
    tasks.append(asyncio.create_task(_run_weekly_cron(
        "learning_report", learning_wd, learning_hour, run_learning_cycle,
        db, guardrails, learning, bybit_alpha, sentiment, notifier,
    )))

    # Paper-only: settle expired positions daily at 16:15
    if hasattr(client, "settle_expired_positions"):
        tasks.append(asyncio.create_task(_run_daily_cron(
            "paper_settle_expired", 16, 15, client.settle_expired_positions,
        )))

    # If any task fails without suppress_exceptions, gather() propagates it
    # (supervision from top-level — no silent scheduler thread death).
    await asyncio.gather(*tasks, return_exceptions=False)


if __name__ == "__main__":
    asyncio.run(main())