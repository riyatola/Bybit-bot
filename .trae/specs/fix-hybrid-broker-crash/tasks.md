# Fix HybridTestnetBroker Crash - Implementation Plan

## [x] Task 1: Add `.body` public attribute/property on shared `_FakeResponse` wrapper (bybit_client.py)
- **Priority**: high (blocks all others)
- **Depends On**: None
- **Description**:
  - In `bybit_client.py` class `_FakeResponse`, add a public attribute or `.body` that returns the same payload `.json()` does (so `self._body). Choose  either (a) `self.body = self._body in __init__ assignment, or (b) a `@property def body(self): return self._body`.
  - Ensure `.json()` unchanged; no external behavior.
- **Acceptance Criteria Addressed**: [AC-1, AC-6]
- **Test Requirements**:
  - programmatic TR-1.1: `_FakeResponse(200, body={"a": 1}).body` returns dict {"a":1}.
  - programmatic TR-1.2: `_FakeResponse(400).body` returns `{}` (default empty object).
  - programmatic TR-1.3: `.json()` and `.body` return equal for same instance after construction.
- **Notes**: ensure test with a quick inline assert via import; no test scripts. Subagent verified all 3 tests PASS.

## [x] Task 2: Fix HybridTestnetBroker merged views (account_details + transactions) to use safe `.json()` / `.body` consistently and always return `_FakeResponse` instead of dict / real dict or fake responses.
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - Fix account_details method in paper_broker.py:
    * When reading real_resp, read via `.json()` helper / or use getter.
    * When reading paper_resp use `.json()` helper.
    * Make sure returned merge result is wrapped returned as `_FakeResponse` object.
  - Fix transactions method.
  - Ensure all AttributeError disappears; tests against the merged positions; include merged securitesAccount; ensure merged `securitiesAccount type HYBRID_TESTNET type consistent with HYBRID_TESTNET; return _FakeResponse; returncode; return dict key securitiesAccount key matches the structure risk_manager expects.
- **Acceptance Criteria Addressed**: [AC-2, AC-3, AC-6]
- **Test Requirements**:
  - programmatic TR-2.1: with stubbed real_client.account_details returns _FakeResponse; hybrid returns a _FakeResponse; risk.account_snapshot reads it via `.json()["securitiesAccount"]
  - programmatic TR-2.2: account_snapshot returns a dict with net_liq float, cash float; positions list; no exceptions raised.
  - programmatic TR-2.3: hybrid.transactions returns _FakeResponse; .json()["transactions"] is list
- **Notes**: trace through crash stack trace line; paper_broker.py L702; wrap all points using `.body` and `.json()` consistent. All 31/31 subagent tests PASS including TR-2.1, TR-2.2, TR-2.3.

## [/] Task 3: Audit + sweep all other Hybrid broker interface methods for same _FakeResponse usage; make sure place_order and any other methods (e.g. settle_expired_positions return types are correct
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - Review place_order response flow (return FakeResponse; including mixed-underlying order responses return _FakeResponse; mixed-order response.
  - Review __FakeResponse constructor used by executor.py handler error bodies with 400/500 cases; ensure body is dict like {"error": ...}"; executor can show error text.
  - Return _FakeResponse object; not plain tuples; check settle_expired_positions doesn't need a return a FakeResponse it already returns float money value.
- **Acceptance Criteria Addressed**: [AC-2, AC-4]
- **Test Requirements**:
  - programmatic TR-3.1: alt-only order via paper place_order returns a FakeResponse; .status_code correct; .json() account_hash right.
  - programmatic TR-3.2: Mixed order place_order returns a proper error FakeResponse
  - programmatic TR-3.3: real-client paper order returns _FakeResponse and .text string; .raise_for_status(); .status_code executor place_order returns response response checks on _FakeResponse or dict; executor accesses via _FakeResponse properly.
- **Notes**: _FakeResponse return types.

## [ ] Task 4: Smoke test script / simple smoke tests / compile all modified files + import and instantiate.
- **Priority**: high
- **Depends On**: Tasks 1-3 all complete
- **Description**:
  - Run `python -m py_compile bybit_client.py paper_broker.py scanner.py trade_tracker.py executor.py risk_manager.py`
  - Simple import-and-instantiate test: build a stubbed BybitClient (testnet=False, no keys should raise if pybit installed) but no network connection required; build HybridTestnetBroker using a local PaperBrokerClient plus stubbed real and stubbed place_order / account_details / transactions lambda response responses are passed; run account_snapshot through risk_manager; transactions; call transactions through hybrid transactions.
- **Acceptance Criteria Addressed**: [AC-2 through AC-6]
- **Test Requirements**:
  - programmatic TR-4.1: six py_compile 0 exit code, no errors.
  - programmatic TR-4.2: import and instantiation hybrid broker (local only; no real bybit call; pass in a stub class) completes 0 AttributeErrors
  - programmatic TR-4.3: risk.account_snapshot(client=Hybrid, account_hash) returns dict keys net_liq, cash, positions list; no exceptions
  - programmatic TR-4.4: transactions() returns dict ["transactions"] list; place_order(altcoin payload) returns FakeResponse status_code 200 (filled paper order).
- **Notes**: Place in a small .py smoke test file under scripts if needed; delete the . after verification for cleanup or keep.
