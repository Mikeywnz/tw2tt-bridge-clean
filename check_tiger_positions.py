from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

# 🛠️ Load Tiger config and client
config = TigerOpenClientConfig()
client = TradeClient(config)

# 🧾 Get recent FUTURES orders for your demo account
orders = client.get_orders(
    account="21807597867063647",  # << your demo account ID
    seg_type=SegmentType.FUT,
    start_date="2025-07-18 20:00:00",
    end_date="2025-07-18 23:59:59",
    limit=100  # ⛔ max 300 allowed — this just caps how many orders return
)

print("📄 Recent TigerTrade Futures Orders:")
if not orders:
    print("⚠️ No orders returned – try widening the time range or verify segment/account.")
else:
    for o in orders:
        print(o)