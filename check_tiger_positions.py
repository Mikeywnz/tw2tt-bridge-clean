from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts.segment_type import SegmentType
from tigeropen.common.consts.order_status import OrderStatus

# ✅ TigerOpen config auto-loads credentials from tiger_openapi_config.properties
config = TigerOpenClientConfig()
client = TradeClient(config)

# 🕒 Get futures orders placed on July 18, 2025
orders = client.get_orders(
    account="21807597867063647",           # ✅ Your demo account ID
    seg_type=SegmentType.FUT,              # 🎯 Futures segment only
    start_time="2025-07-17",               # 📅 Start of time range (whole day)
    end_time="2025-07-18",                 # 📅 End of time range
    states=["Filled", "Cancelled"],        # 📦 We want both filled + cancelled
    limit=100                              # ⛔ Max 300 allowed
)

# 📢 Print results
print("📄 Recent TigerTrade Futures Orders:")
if not orders:
    print("⚠️ No orders returned — try widening the time range or check filters.")
else:
    for o in orders:
        print(o)