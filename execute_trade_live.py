import sys
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
# Print the connected account to confirm
assets = client.get_assets()
account_id = assets[0].account_id
if isinstance(assets, list) and len(assets) > 0:
    print("📋 Connected account:", assets[0].account_id)
else:
    print("⚠️ No account found in assets response:", assets)

# ✅ Step 3: Build futures contract
contract = future_contract(symbol=symbol, currency='USD')

# ✅ Step 4: Create Market Order
order = Order(account_id, contract, action)
order.order_type = "MKT"
order.quantity = quantity
order.outside_rth = False

# 🔍 Log contract and order for debugging
print("📦 Contract details:", contract)
print("📝 Order details:", order)

# ✅ Step 5: Submit Order
try:
    response = client.place_order(order)
    print("✅ Order submitted. Response:", response)
except Exception as e:
    print("❌ Error placing order:", e)