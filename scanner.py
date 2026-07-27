"""
Main entrypoint for crypto options bot (Bybit).
Wires together: auth, market data, strategies, risk, approval, notifier, learning, guardrails, bybit_alpha, sentiment, circuit breaker.
"""

import asyncio
import argparse
import logging
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

def _configure_logging():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("scanner")


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def get_universe(cfg):
    """Static list of underlyings to scan."""
    return cfg.get("universe", {}).get("symbols", ["BTC", "ETH"])


async def run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier,
                         guardrails, learning, bybit_alpha, sentiment, circuit_breaker):
    db.expire_stale()

    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping scan")
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


async def run_exit_check(exit_manager, db, notifier, circuit_breaker):
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
            log.info("Auth successful — Bybit API key accepted.")
        except Exception as e:
            log.error("Auth failed: %s", _format_api_error(e, testnet=bybit_cfg.get("testnet", True)))
            sys.exit(1)
        return

    # Paper wrapper if mode is paper
    if cfg.get("mode", "paper") == "paper":
        client = PaperBrokerClient(real_client, cfg)
        account_hash = "PAPER0"  # paper broker uses fixed hash
    else:
        client = real_client
        # For real, we need an account hash – Bybit uses account ID? We'll use a dummy for now.
        account_hash = "REAL_ACCOUNT"  # placeholder; adapt as needed

    db = Db()
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
    await notifier.send_note("Crypto options scanner started.")

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