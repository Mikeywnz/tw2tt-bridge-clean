from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

# === Tiger API Config ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Get recent orders ===
orders = client.get_orders()

print("📄 Recent TigerTrade orders:")
if not orders:
    print("⚠️ No orders returned — check account mode or activity.")
else:
    for o in orders:
        print(f"🔸 Order ID: {o.order_id}")
        print(f"   Symbol: {o.symbol}")
        print(f"   Action: {o.action}")
        print(f"   Status: {o.status}")
        print(f"   Quantity: {o.quantity}")
        print(f"   Filled: {o.filled_quantity} @ Avg Price: {o.filled_avg_price}")
        print("-" * 40)