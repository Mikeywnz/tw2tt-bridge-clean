from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from datetime import datetime, timedelta

config = TigerOpenClientConfig()
client = TradeClient(config)

# 🕒 Create 50-minute rolling window
end_time = datetime.utcnow()
start_time = end_time - timedelta(minutes=50)

start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

# 🔍 Get Filled + Cancelled Orders
orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time=start_str,
    end_time=end_str,
    states=["Filled", "Cancelled"],
    limit=100
)

print(f"\n⏱️ Order Window: {start_str} to {end_str}")
print("📄 Orders Returned:")

if not orders:
    print("⚠️ No orders returned — test window may be empty or API rejected timeframe.")
else:
    for o in orders:
        print(o)

# 🔍 Also check current open positions
positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)

print("\n📊 Open Positions:")
if not positions:
    print("⚠️ No open futures positions.")
else:
    for p in positions:
        print(p)