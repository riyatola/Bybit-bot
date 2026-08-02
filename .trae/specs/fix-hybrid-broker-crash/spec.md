# Fix HybridTestnetBroker - Product Requirement Document

## Overview
- **Summary**: Fix the startup crash in BOT_ALTCOIN_MOCK=1 HybridTestnetBroker path caused by mismatched `_FakeResponse` attribute access, plus sweep all remaining interface mismatches between the Hybrid broker wrapper, `PaperBrokerClient`, `BybitClient`, and the four downstream consumers (risk_manager, executor, trade_tracker, scanner).
- **Purpose**: The Render deploy from 2026-08-01 crashes on first scan cycle at `HybridTestnetBroker.account_details` with `AttributeError: '_FakeResponse' object has no attribute 'body'`. Every scan cycle aborts before any candidates are evaluated, so the hybrid training mode is totally non-functional. This PRD fixes the crash and makes the hybrid broker interface 100% compatible with all code that calls it.
- **Target Users**: Crypto-Options-Bot maintainer/operator running Render deployment.

## Goals
- **G-1**: Eliminate the AttributeError crash on scan cycle startup so the bot can actually scan, generate candidates, and execute trades with `BOT_ALTCOIN_MOCK=1`.
- **G-2**: Make `HybridTestnetBroker` method signatures and return values (including `_FakeResponse` usage) are identical to the combined surface used by all callers, so no follow-on interface bugs don't re-occur on `transactions`, `place_order`, `account_details`, `_fetch_derivative_positions` etc.
- **G-3**: Add a public `.body` convenience property on the shared `_FakeResponse` wrapper so existing code that reads `.body` (like executor reads work instead of crashing.
- **G-4**: Add a smoke-level self-test script / compile check that exercises Hybrid broker's `account_details`, `transactions`, and `place_order` surface without needing Bybit auth.

## Non-Goals (Out of Scope)
- Not changing BTC/ETH routing (Bybit pass-through path logic or testnet/live symbol whitelist outside Hybrid mode.
- Not changing BybitClient/PaperBrokerClient business logic beyond interface compatibility wrappers.
- Not refactoring account_hash / broker selection logic in scanner.py beyond what's needed for the crash.
- Not adding new strategy features; purely a stabilisation/bugfix patch.

## Background & Context
- Existing `_FakeResponse` class in bybit_client.py stores payload in `self._body` private attr and exposes via public `.json()` method, `.text` property, `.raise_for_status()`, `.status_code`, `.headers`. No public `.body` attr existed before.
- `risk_manager.account_snapshot()` calls `client.account_details(...)` then calls `.json()` on the returned response (works), which all callers like `PaperBrokerClient` and `BybitClient` return `_FakeResponse` instances so this used directly.
- HybridTestnetBroker (new in BOT_ALTCOIN_MOCK=1) internally calls merged account_details reads `.body` on both `real_resp.body` and `paper_resp.body` — two locations in the crash is in L702. paper_broker.py. Same pattern repeats in `transactions()` method. AttributeError.
- Render logs confirm every scan `CRASHED:` `'_FakeResponse' object has no attribute 'body'`.
- The error in each scan tick completes in paper_broker.py L702.
- The broker interface is also used in trade_tracker.py, executor.py and scanner.py so any interface asymmetry will introduce other subtle bugs if not fully corrected.

## Functional Requirements
- **FR-1**: `_FakeResponse` exposes `.body` public attribute (either via direct attribute or property returning same value as `.json()` returns). Backward compatible; existing callers work unchanged.
- **FR-2**: `HybridTestnetBroker.account_details(account_hash, fields)` returns a `_FakeResponse` (not a dict or requests.Response-lookalike) object) that callers (risk_manager, etc) call `.json()`, `.raise_for_status()`, `.status_code` work without errors.
- **FR-3**: `HybridTestnetBroker.transactions(...)` returns a `_FakeResponse` where `.json()` returns the shape trade_tracker expects (dict with key `"transactions"`: list). And internal reads consistent.
- **FR-4**: Hybrid broker uses `.json()` (or `.body` after we add the property) consistently. Never accesses private `._body` directly from outside. All response objects; never reads `.body` from responses from PaperBrokerClient/BybitClient without first verifying attribute exists / using safe accessor.
- **FR-5**: Scanner startup smoke checks pass. Py-compile: paper_broker.py, scanner.py, trade_tracker.py, executor.py, bybit_client.py all compile after changes.
- **FR-6**: Hybrid mode's `_FakeResponse` objects returned from merged methods always have `retCode == 0`-style bodies where applicable (in line with PaperBrokerClient wrapper conventions).

## Non-Functional Requirements
- **NFR-1**: Zero new runtime external dependencies; everything stays in stdlib + pybit already in requirements.
- **NFR-2**: The fix must not alter the BTC/ETH pass-through data content previously used by operators not using Hybrid mode.
- **NFR-3**: Log lines already in Hybrid broker log lines are not removed; errors surface cleanly, but they must not be raised in non-Hybrid PaperBrokerClient/BybitClient paths.

## Constraints
- **Technical**: Python 3.14+, Render Linux runtime; SQLite/Turso persistence; _FakeResponse only  surface frozen for callers. Cannot change the call to  call signatures of PaperBrokerClient/BybitClient. The existing interfaces in this repo.
- **Business**: Fix within one Render deploy cycle. No schema migrations altering trade records already written. The `_FakeResponse` wrapper body is private.
- **Dependencies**: Shared `_FakeResponse` is in bybit_client.py and PaperBrokerClient in paper_broker.py imports it.

## Assumptions
- All downstream consumers (risk_manager, executor, trade_tracker, scanner) ONLY rely on `.json()`, `.raise_for_status()`, `.status_code`, `.text`, `.headers`, `.body` once we add it.
- Hybrid broker using `BOT_ALTCOIN_MOCK=1 path in Render doesn't need pybit for the smoke test; PaperBrokerClient can be initialised with a stub Real BybitClient instance that throws benign errors for market-data calls during the test.

## Acceptance Criteria

### AC-1: Hybrid broker interface attr attribute is public on _FakeResponse
- **Given**: existing _FakeResponse usage anywhere
- **When**: code reads `resp.body`
- **Then**: it returns the payload equivalent to `resp.json()`; no AttributeError.
- **Verification**: programmatic
- **Notes**: Property-based property is fine; direct attribute assignment in __init__ is also fine.

### AC-2: No AttributeError in `run_scan_cycle` under BOT_ALTCOIN_MOCK=1 path
- **Given**: scanner boot with `BOT_ALTCOIN_MOCK=1`, hybrid broker initialised, no network (offline smoke)
- **When**: `risk.account_snapshot(client, account_hash)` is called
- **Then**: returns a valid snapshot dict; no exceptions; net_liq/cash_available are floats; positions is list.
- **Verification**: programmatic

### AC-3: Hybrid.transactions() returns data in trade_tracker-compatible shape
- **Given**: hybrid broker initialised
- **When**: transactions(account_hash) called
- **Then**: `.json()["transactions"]` is a list; may be empty; no AttributeError; each dict can have `"hybrid_mock"` tag.
- **Verification**: programmatic

### AC-4: All modules compile cleanly; bybit_client + paper_broker's _FakeResponse interface symmetry after patch
- **Given**: All modified files
- **When**: `python -m py_compile` + import
- **Then**: zero syntax errors
- **Verification**: programmatic

### AC-5: Smoke self-test
- **Given**: synthetic test harness/stubbed real_client
- **When**: instantiate Hybrid broker, call account_details/transactions; risk_manager.account_snapshot on top
- **Then**: return shapes exactly match existing test requirements
- **Verification**: programmatic
- **Notes**: No actual network; stub the real_client account_details with a lambda.

### AC-6: Existing non-hybrid path regressions
- **Given**: Pure paper or BybitClient paths
- **When**: existing test or import
- **Then**: no code does not change BTC/ETH or normal paper paths behavior
- **Verification**: programmatic / human by comparing _FakeResponse in bybit_client before/after: old code that does `.json()` continues return the same.

## Open Questions
- [ ] None open. Root cause isolated.
