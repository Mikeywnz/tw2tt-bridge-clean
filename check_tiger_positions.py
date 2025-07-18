from tigeropen.common.consts.segment_type import SegmentType
from tigeropen.common.consts.order_status import OrderStatus  # 👈 you may need this too

orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    start_time="2025-07-17",
    end_time="2025-07-18",
    states=["Filled", "Cancelled"],  # 👈 crucial to see historical orders
    limit=100
)