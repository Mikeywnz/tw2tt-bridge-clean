from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from datetime import datetime, timedelta

# ✅ Load config and connect to client
config = TigerOpenClientConfig()
client = TradeClient(config)

# ✅ Define your account ID
account_id = "21807597867063647"

# ✅ Set time window: past 2 hours
start_time = (datetime.utcnow() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
end_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

# ✅ Request recent orders
orders = client.get_orders(
    account=account_id,
    start_date=start_time,
    end_date=end_time
)

# ✅ Show result
print(f"✅ Orders from Tiger (last 2 hrs):\n{orders}")