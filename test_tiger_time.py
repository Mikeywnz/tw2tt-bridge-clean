from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from datetime import datetime, timedelta

config = TigerOpenClientConfig()
client = TradeClient(config)

now = datetime.utcnow()
start = now - timedelta(hours=1)

orders = client.get_orders(
    account="21807597867063647",  
    seg_type=SegmentType.FUT,
    start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
    end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
    limit=10
)

for order in orders:
    print("---")
    print("Order ID:", getattr(order, 'id', ''))
    print("Order Time (raw):", getattr(order, 'order_time', ''))
    print("Avg Fill Price:", getattr(order, 'avg_fill_price', ''))
    print("Status:", getattr(order, 'status', ''))