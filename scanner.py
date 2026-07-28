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
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bybit_client import BybitClient, _format_api_error
from market_data import MarketDataAdapter
from paper_broker import PaperBrokerClient
from risk_manager import RiskManager
from approval_manager import Db
from notifier import TelegramNotifier
from executor import Executor
from strategies import credit_spreads, directional, earnings_vol, wheel
from guardrails import Guardrails
from learning import LearningEngine
from sentiment import SentimentEngine
from bybit_alpha import BybitAlphaEngine
from circuit_breaker import CircuitBreaker
from exit_manager import ExitManager
from trade_tracker import TradeTracker
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
            "universe": {"symbols": ["BTC", "ETH"], "wheel_watchlist": ["BTC", "ETH"], "min_avg_option_volume": 0},
            "scan": {"poll_interval_seconds": 900},
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
            "bybit_alpha": {"enabled": True, "cache_ttl_seconds": 900, "persistence_readings": 3,
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
                "manual_kill": False, "check_interval_seconds": 900},
            "exits": {"enabled": True,
                "default_stop_loss_pct": {"credit_spreads": 0.50, "directional": 0.50, "wheel": 0.50},
                "default_take_profit_pct": {"credit_spreads": 0.50, "directional": 1.00, "wheel": 0.50},
                "force_exit_dte": 7, "approval_timeout_minutes": 10},
        }
    return _overlay_env_config(cfg)


def get_universe(cfg):
    """Static list of underlyings to scan."""
    return cfg.get("universe", {}).get("symbols", ["BTC", "ETH"])


async def run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier,
                         guardrails, learning, bybit_alpha, sentiment, circuit_breaker):
    db.expire_stale()

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

        approval_id = db.create_approval(candidate, cfg["approval"]["timeout_minutes"])
        await notifier.send_approval_request(approval_id, candidate)
        log.info("Sent approval %s for %s (%s), score=%.2f",
                 approval_id, candidate["symbol"], candidate["strategy"], candidate["score"])

    mark_event("scan")


async def run_exit_check(exit_manager, db, notifier, circuit_breaker):
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
            await notifier.send_approval_request(approval_id, candidate)
            log.info(f"Sent exit approval for trade {trigger['trade_id']}")
    finally:
        mark_event("exit_check")


async def run_learning_cycle(db, guardrails, learning, bybit_alpha, sentiment, notifier):
    newly_banned = guardrails.evaluate_all_symbols()
    learning.maybe_retrain_ml_model()
    summary = learning.summarize() + "\n\n" + bybit_alpha.summarize() + "\n\n" + sentiment.summarize()
    active_bans = guardrails.list_active_bans()
    await notifier.send_learning_report(summary, newly_banned, active_bans)


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

    await notifier.run_polling_forever()
    mark_event("telegram")
    await notifier.send_note("Crypto options scanner started.")
    log.info("Bot online. Health endpoint: http://0.0.0.0:%d/healthz", get_port())

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_scan_cycle,
        "interval",
        seconds=cfg["scan"]["poll_interval_seconds"],
        args=[client, account_hash, cfg, market_data, risk, db, notifier,
              guardrails, learning, bybit_alpha, sentiment, circuit_breaker],
    )

    scheduler.add_job(
        run_exit_check,
        "interval",
        seconds=cfg["scan"]["poll_interval_seconds"],
        args=[exit_manager, db, notifier, circuit_breaker],
    )

    scheduler.add_job(
        trade_tracker.poll_and_update_outcomes,
        "interval",
        minutes=cfg.get("learning", {}).get("outcome_poll_minutes", 45),
    )

    scheduler.add_job(
        run_learning_cycle,
        "cron",
        day_of_week=cfg.get("learning", {}).get("report_day_of_week", "sun"),
        hour=cfg.get("learning", {}).get("report_hour", 18),
        args=[db, guardrails, learning, bybit_alpha, sentiment, notifier],
    )

    # Paper-only: settle expired positions daily (no early assignment)
    if hasattr(client, "settle_expired_positions"):
        scheduler.add_job(client.settle_expired_positions, "cron", hour=16, minute=15)  # adjust time

    scheduler.start()

    # Run initial scan
    await run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier,
                         guardrails, learning, bybit_alpha, sentiment, circuit_breaker)

    # Keep running
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())