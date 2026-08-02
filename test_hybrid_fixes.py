import sys
import py_compile

from bybit_client import _FakeResponse
from paper_broker import PaperBrokerClient, HybridTestnetBroker

passed = 0
failed = 0

def check(name: str, condition: bool):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1

print("=" * 60)
print("STEP (a): py_compile check on paper_broker.py")
print("=" * 60)
try:
    py_compile.compile("paper_broker.py", doraise=True)
    check("paper_broker.py compiles without SyntaxError", True)
except py_compile.PyCompileError as e:
    print(f"  Compile error: {e}")
    check("paper_broker.py compiles without SyntaxError", False)

print()
print("=" * 60)
print("STEP (b): Construct stub real_client")
print("=" * 60)

class StubRealClient:
    def account_details(self, account_hash: str, fields: str = "positions"):
        return _FakeResponse(200, body={
            "securitiesAccount": {
                "positions": [
                    {
                        "instrument": {"symbol": "BTC-15AUG25-100000-C"}
                    }
                ],
                "cash_available": 5000,
                "net_liq": 10000,
                "cash": 6000,
                "liquidationValue": 9000,
                "roundTrips": 0,
                "isDayTrader": False,
                "type": "BYBIT_TESTNET",
                "accountNumber": "BYBIT-TESTNET"
            }
        })

    def transactions(self, account_hash: str, symbol=None, types="TRADE",
                     start_date=None, end_date=None):
        return _FakeResponse(200, body={
            "transactions": [{"netAmount": 100}]
        })

    def get_option_chain_bybit(self, underlying, category="option", base_coin=None):
        return []

    def get_option_tickers(self, base_coin="BTC"):
        return []

    def get_linear_tickers(self, category="linear"):
        return [
            {"symbol": "BTCUSDT", "markPrice": "65000.0", "lastPrice": "65000.0"},
            {"symbol": "ETHUSDT", "markPrice": "3500.0", "lastPrice": "3500.0"},
            {"symbol": "SOLUSDT", "markPrice": "150.0", "lastPrice": "150.0"},
            {"symbol": "BNBUSDT", "markPrice": "600.0", "lastPrice": "600.0"},
            {"symbol": "XRPUSDT", "markPrice": "0.5", "lastPrice": "0.5"},
            {"symbol": "DOGEUSDT", "markPrice": "0.15", "lastPrice": "0.15"},
            {"symbol": "ADAUSDT", "markPrice": "0.4", "lastPrice": "0.4"},
            {"symbol": "AVAXUSDT", "markPrice": "35.0", "lastPrice": "35.0"},
            {"symbol": "MATICUSDT", "markPrice": "0.8", "lastPrice": "0.8"},
            {"symbol": "DOTUSDT", "markPrice": "7.0", "lastPrice": "7.0"},
        ]

    def get_linear_ticker(self, symbol):
        return None

    def get_long_short_ratio(self, symbol, period="1h", limit=3):
        return []

    def account_info(self):
        return {"mode": "stub-real"}

    def place_order(self, account_hash, payload):
        return _FakeResponse(201, body={"order_id": "STUB-001"})

real_client = StubRealClient()
check("StubRealClient instantiated", isinstance(real_client, StubRealClient))

real_acc_resp = real_client.account_details("hash")
check("stub account_details returns _FakeResponse", isinstance(real_acc_resp, _FakeResponse))
check("stub account_details .json() has securitiesAccount", "securitiesAccount" in real_acc_resp.json())
check("stub account_details has 1 position", len(real_acc_resp.json()["securitiesAccount"]["positions"]) == 1)

real_txn_resp = real_client.transactions("hash")
check("stub transactions returns _FakeResponse", isinstance(real_txn_resp, _FakeResponse))
check("stub transactions .json() has 1 txn", len(real_txn_resp.json()["transactions"]) == 1)

print()
print("=" * 60)
print("STEP (c): Construct Hybrid broker")
print("=" * 60)

cfg = {"paper": {"starting_cash": 10000.0, "slippage_bps": 5, "commission_per_contract": 0.0}}

try:
    hybrid = HybridTestnetBroker(real_client, cfg)
    check("HybridTestnetBroker instantiated without exception", True)
    check("hybrid.real is our stub real_client", hybrid.real is real_client)
    check("hybrid.alt_paper is PaperBrokerClient", isinstance(hybrid.alt_paper, PaperBrokerClient))
except Exception as e:
    print(f"  Error constructing hybrid: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    check("HybridTestnetBroker instantiated without exception", False)

print()
print("=" * 60)
print("STEP (d): Call hybrid.account_details and verify")
print("=" * 60)

try:
    acc_resp = hybrid.account_details("BYBIT-TESTNET-HYBRID", fields="positions")
    check("account_details call succeeds without exception", True)
except Exception as e:
    print(f"  Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    acc_resp = None
    check("account_details call succeeds without exception", False)

if acc_resp is not None:
    check("returns _FakeResponse", isinstance(acc_resp, _FakeResponse))
    check("status_code == 200", getattr(acc_resp, "status_code", None) == 200)

    try:
        acc_json = acc_resp.json()
        check(".json() is callable and returns dict", isinstance(acc_json, dict))
    except Exception as e:
        print(f"  .json() error: {e}")
        acc_json = None
        check(".json() is callable and returns dict", False)

    if acc_json is not None:
        sec = acc_json.get("securitiesAccount")
        check("securitiesAccount key exists", sec is not None and isinstance(sec, dict))
        if sec is not None:
            check("securitiesAccount.type == 'HYBRID_TESTNET'", sec.get("type") == "HYBRID_TESTNET")

            positions = sec.get("positions", [])
            check("positions is list with len >= 1", isinstance(positions, list) and len(positions) >= 1)

            cash_available = sec.get("cash_available")
            check("cash_available is float", isinstance(cash_available, float))
            check("cash_available == 5000 (real) + 10000 (paper starting) = 15000", cash_available == 15000.0)

            net_liq = sec.get("net_liq")
            check("net_liq is float", isinstance(net_liq, float))
            check("net_liq >= 0 (positive number)", isinstance(net_liq, (int, float)) and net_liq >= 0)

            cash = sec.get("cash")
            check("cash is float", isinstance(cash, float))

            account_number = sec.get("accountNumber")
            check("accountNumber is hybrid hash", account_number == "BYBIT-TESTNET-HYBRID")

print()
print("=" * 60)
print("STEP (e): Call hybrid.transactions and verify")
print("=" * 60)

try:
    txn_resp = hybrid.transactions("BYBIT-TESTNET-HYBRID")
    check("transactions call succeeds without exception", True)
except Exception as e:
    print(f"  Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    txn_resp = None
    check("transactions call succeeds without exception", False)

if txn_resp is not None:
    check("returns _FakeResponse", isinstance(txn_resp, _FakeResponse))
    check("status_code == 200", getattr(txn_resp, "status_code", None) == 200)

    try:
        txn_json = txn_resp.json()
        check(".json() is callable and returns dict", isinstance(txn_json, dict))
    except Exception as e:
        print(f"  .json() error: {e}")
        txn_json = None
        check(".json() is callable and returns dict", False)

    if txn_json is not None:
        txns = txn_json.get("transactions")
        check('.json()["transactions"] is list', isinstance(txns, list))
        if isinstance(txns, list):
            check("transactions list has at least 1 item (from real client)", len(txns) >= 1)

            has_real_net_amount_100 = any(
                isinstance(t, dict) and t.get("netAmount") == 100 for t in txns
            )
            check("real client txn with netAmount=100 is present", has_real_net_amount_100)

            has_hybrid_mock = any(
                isinstance(t, dict) and t.get("hybrid_mock") is True for t in txns
            )
            note = "" if has_hybrid_mock else " (paper broker has 0 txns yet, so hybrid_mock tagged list is absent — that's OK per spec: 'hybrid_mock tags OR existing list')"
            check(f"either hybrid_mock tags exist OR real txn list is present{note}", has_hybrid_mock or has_real_net_amount_100)

print()
print("=" * 60)
print(f"SUMMARY: {passed} PASSED, {failed} FAILED")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
