from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus

orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time="2025-07-17",
    end_time="2025-07-18",
    states=["Filled", "Cancelled"],  # 👈 crucial to see historical orders
    limit=100
)