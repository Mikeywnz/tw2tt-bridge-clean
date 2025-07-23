from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from datetime import datetime, timedelta

# === Load config and connect ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Expanded time window: last 48 hours ===
now = datetime.utcnow()
start = now - timedelta(hours=48)

orders = client.get_orders(
    account="21807597867063647",   # Your demo account
    seg_type=SegmentType.FUT,
    start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
    end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
    limit=50
)

# === Confirm orders received ===
if not orders:
    print("⚠️ No orders returned from TigerTrade.")
else:
    for order in orders:
        print("\n---")
        print("Order ID:       ", getattr(order, 'id', ''))
        print("Symbol:         ", getattr(order, 'contract', ''))
        print("Status:         ", getattr(order, 'status', ''))
        raw_ts = getattr(order, 'order_time', '')
        print("Order Time Raw: ", raw_ts)
        try:
            ts = int(raw_ts)
            if ts > 1e12:
                ts //= 1000
            print("🔍 Interpreted: ", datetime.utcfromtimestamp(ts).isoformat())
        except Exception as e:
            print("⚠️ Timestamp error:", e)