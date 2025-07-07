import sys
import os
import json
import csv
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

# ✅ Step 5: Submit Order
try:
    response = client.place_order(order)
    print("✅ Order submitted. Response:", response)

    # === STEP 6: Get current price from live_prices.json ===
    live_price = 0.0
    try:
        with open(os.path.join(os.path.dirname(__file__), 'live_prices.json')) as f:
            live_data = json.load(f)
            live_price = float(live_data.get(symbol, 0.0))
    except Exception as e:
        print("⚠️ Could not read live_prices.json:", e)

    # === STEP 7: Get EMA values from ema_values.json ===
    ema9, ema20 = "", ""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'ema_values.json')) as f:
            ema_data = json.load(f)
            ema9 = ema_data.get("ema9", "")
            ema20 = ema_data.get("ema20", "")
    except Exception as e:
        print("⚠️ Could not read ema_values.json:", e)

    # === STEP 8: Append to open_trades.csv in /src ===
    row = [
        symbol,
        live_price,
        action,
        1,        # contracts_remaining
        1.0,      # trail_perc
        0.5,      # trail_offset
        '',       # tp_trail_price (initially blank)
        ema9,
        ema20
    ]
    try:
        # ✅ FIXED HERE:
        csv_path = os.path.join(os.path.dirname(__file__), 'src', 'open_trades.csv')
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
        print("📌 Trade logged to open_trades.csv:", row)
    except Exception as e:
        print("❌ Error writing to open_trades.csv:", e)

except Exception as e:
    print("❌ Error placing order:", e)