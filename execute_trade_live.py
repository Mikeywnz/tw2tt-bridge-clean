import sys
import os
import json
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.contract.futures import FuturesContract
from tigeropen.trade.domain.order import Order

# ✅ Step 1: Parse args
if len(sys.argv) != 4:
    print("Usage: python3 execute_trade_live.py <symbol> <buy/sell> <quantity>")
    sys.exit(1)

symbol = sys.argv[1]
action = sys.argv[2].upper()
quantity = int(sys.argv[3])

print(f"📂 Executing Trade → Symbol: {symbol}, Action: {action}, Quantity: {quantity}")

# ✅ Step 2: Load config and client
try:
    config_path = '/etc/secrets/tiger_openapi_config.properties'
    config = TigerOpenClientConfig(config_path)
    if not config.account:
        raise ValueError("Tiger config loaded but account is missing or blank.")
    client = TradeClient(config)
    client.open_trade_session()
except Exception as e:
    print(f"❌ Failed to load Tiger API config or initialize client: {e}")
    sys.exit(1)

# ✅ Step 3: Manually Build Futures Contract
contract = FuturesContract()
contract.symbol = symbol
contract.currency = "USD"
contract.exchange = "CME"
contract.contract_type = "FUT"

# ✅ Step 4: Create Market Order
order = Order(config.account, contract, action)
order.order_type = "MKT"
order.limit_price = None
order.quantity = quantity
order.outside_rth = False

# ✅ Step 5: Submit Order and detect fill
try:
    print("🧾 Contract Details:", contract.__dict__)
    sys.stdout.flush()

    response = client.place_order(order)
    response = client.place_order(order)

    print("✅ Order submitted. Raw Response:", response.__dict__)
    if hasattr(response, 'message'):
        print("❗Tiger response message:", response.message)

    # === STEP 5B: Check fill status ===
    order_status = getattr(response, "status", "UNKNOWN")
    filled_qty = getattr(response, "filled", 0)
    is_filled = order_status == "Filled" or filled_qty > 0
    filled_str = "true" if is_filled else "false"

    # === STEP 6: Get current price from live_prices.json ===
    live_price = 0.0
    try:
        with open(os.path.join(os.path.dirname(__file__), 'live_prices.json')) as f:
            live_data = json.load(f)
            if isinstance(live_data.get(symbol), dict):
                live_price = float(live_data.get(symbol, {}).get("price", 0.0))
            else:
                live_price = float(live_data.get(symbol, 0.0))
    except Exception as e:
        print("⚠️ Could not read live_prices.json:", e)

    # === STEP 7: Create timestamp ===
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if is_filled:
        print(f"✅ Trade confirmed filled at approx. ${live_price} – timestamp {timestamp}")
    else:
        print("⚠️ Order not filled – no further logging will occur.")

except Exception as e:
    print("❌ Error placing order:", e)
    sys.exit(1)