from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!

config = TigerOpenClientConfig()
client = TradeClient(config)

orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time="2025-07-17",
    end_time="2025-07-19",
    #states=["Cancelled"],    #states=["Filled", "Cancelled"],
    limit=100
)

print("📄 Recent TigerTrade Futures Orders:")
if not orders:
    print("⚠️ No orders returned — try widening the time range or check filters.")
else:   #this section working now to get Filled orders cancelled order and rejected orders (which Tiger calls Expired if "EXPIRED" in status and "资金" in reason:)
    for o in orders:
        status = str(getattr(o, "status", "")).upper()
        reason = str(getattr(o, "reason", "")).upper()

        if "EXPIRED" in status and "资金" in reason:
            print("🚫 Rejected order (margin failure):")
            print(o)

        elif "CANCELLED" in status:
            print("❌ Manually cancelled order:")
            print(o)

        else:
            print(f"❓ Unknown status: {status} | reason: {reason}")
            print(o)


# 🔍 Also check current open positions
positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)

print("\n📊 Open Positions:")
if not positions:
    print("⚠️ No open futures positions.")
else:
    for p in positions:
        print(p)