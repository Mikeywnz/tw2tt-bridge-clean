from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!

config = TigerOpenClientConfig()
client = TradeClient(config)

orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time="2025-07-14",
    end_time="2025-07-19",
    states=["Cancelled"],    #states=["Filled", "Cancelled"],
    limit=100
)

print("📄 Recent TigerTrade Futures Orders:")
if not orders:
    print("⚠️ No orders returned — try widening the time range or check filters.")
else:
    for o in orders:
        status = o.get("status", "").upper()

        if status == "REJECTED":
            print("🚫 Rejected order (likely margin fail):")
        elif status == "CANCELLED":
            print("❌ Manually cancelled order:")
        else:
            print(f"❓ Unknown status: {status}")

        print(o)  # Always print full order after header