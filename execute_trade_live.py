import sys
import os
import json
import csv
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.util.contract_utils import future_contract
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
config = TigerOpenClientConfig()
client = TradeClient(config)

# ✅ Step 3: Build futures contract
contract = future_contract(symbol=symbol, currency='USD')

# ✅ Step 4: Create Market Order
order = Order(config.account, contract, action)
order.order_type = "MKT"
order.quantity = quantity
order.outside_rth = False

# ✅ Step 5: Submit Order and detect fill
try:
    response = client.place_order(order)
    print("✅ Order submitted. Response:", response)

    # === STEP 5B: Check fill status ===
    order_status = getattr(response, "status", "UNKNOWN")
    filled_qty = getattr(response, "filled", 0)
    is_filled = order_status in ["Submitted", "Filled"] or filled_qty > 0
    filled_str = "true" if is_filled else "false"

    # === STEP 6: Get current price from live_prices.json ===
    live_price = 0.0
    ema50 = ""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'live_prices.json')) as f:
            live_data = json.load(f)
            live_price = float(live_data.get(symbol, 0.0))
            ema50 = live_data.get("ema50", "")
    except Exception as e:
        print("⚠️ Could not read live_prices.json:", e)

    # === STEP 7: Create timestamp ===
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # === STEP 8: Append to open_trades.csv ===
    row = [
        symbol,
        live_price,
        action,
        1,        # contracts_remaining
        0.4,      # trail_perc
        0.2,      # trail_offset
        '',       # tp_trail_price
        ema50,
        filled_str,
        timestamp
    ]
    try:
        csv_path = os.path.join(os.path.dirname(__file__), 'open_trades.csv')
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
        print("📌 Trade logged to open_trades.csv:", row)
    except Exception as e:
        print("❌ Error writing to open_trades.csv:", e)

except Exception as e:
    print("❌ Error placing order:", e)