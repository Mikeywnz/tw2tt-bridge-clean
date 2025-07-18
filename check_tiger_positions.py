from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

config = TigerOpenClientConfig()
client = TradeClient(config)

# ✅ Use your actual account ID — confirmed from get_assets() output
account_id = "21807597867063647"

orders = client.get_orders(account=account_id)
print("📄 Recent TigerTrade orders:")

if not orders:
    print("⚠️ No orders returned — check account mode, filters, or time range.")
else:
    for o in orders:
        print(o)