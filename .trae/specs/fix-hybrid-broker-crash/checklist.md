# Verification Checklist - Fix HybridTestnetBroker _FakeResponse body AttributeError

- [ ] Checkpoint 1: `_FakeResponse(200, body={"a":1}).body` returns `{"a":1}` (not raises AttributeError). `_FakeResponse` in bybit_client.py)
- [ ] Checkpoint 2: `_FakeResponse(400).body` returns `{}` (default empty)
- [ ] Checkpoint 3: `_FakeResponse(...).json()` and `.body` return equal values for a single instance (backwards compatibility); `.raise_for_status still works on the same object
- [ ] Checkpoint 4: HybridTestnetBroker.account_details(account_hash, fields="positions") returns a `_FakeResponse` object (not dict) whose `.json()["securitiesAccount"]` with `accountNumber`, `type: HYBRID_TESTNET`, `positions` list, `cash_available` float, `net_liq` float, `cash` float.
- [ ] Checkpoint 5: `risk_manager.account_snapshot()` using Hybrid broker against hybrid.account_details -> returns flat snapshot with keys `positions`, `net_liq`, `cash_available`, `symbols_open` set, no AttributeError.
- [ ] Checkpoint 6: `HybridTestnetBroker.transactions(account_hash)` returns a `_FakeResponse` with `.json()["transactions"]` list (list type).
- [ ] Checkpoint 7: `HybridTestnetBroker.place_order` for altcoin-only order returns a 200 _FakeResponse; `.status_code == 200`; filled paper order id.
- [ ] Checkpoint 8: Mixed underlyings order (should be a proper 400 FakeResponse error JSON `HYBRID_MIXED_UNDERLYINGS error code)
- [ ] Checkpoint 9: Live-only (BTC/ETH) place_order delegated to real client; response through without error; result in an object without error; properly wrapped FakeResponse if real client is stubbed or dict if not when we need FakeResponse consistent.
- [ ] Checkpoint 10: `python -m py_compile bybit_client.py paper_broker.py scanner.py risk_manager.py trade_tracker.py executor.py ` zero exit code no syntax errors.
- [ ] Checkpoint 11: All existing non-hybrid call sites unchanged: PaperBrokerClient account_details; transactions FakeResponse returned from paper broker no AttributeError accessing .body when we now read .json() (or after fix); no crashes in bybit normal paths.
- [ ] Checkpoint 12: merged `_fetch_derivative_positions returns list dict shape (perps merged no duplicates symbols correctly).
