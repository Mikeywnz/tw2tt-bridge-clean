from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from datetime import datetime, timedelta

config = TigerOpenClientConfig()
client = TradeClient(config)

# 🕒 Create 50-minute window ending now
end_time = datetime.utcnow()
start_time = end_time - timedelta(minutes=50)

# Format with time string (must be exact!)
start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

# 🔍 Query recent orders using time-stamped window
orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time=start_str,
    end_time=end_str,
    states=["Filled", "Cancelled"],
    limit=100
)

print(f"\n⏱️ Window: {start_str} to {end_str}")
print("📄 Orders Returned:")

if not orders:
    print("⚠️ No orders returned — test window may be empty or API rejected timeframe.")
else:
    for o in orders:
        print(o)