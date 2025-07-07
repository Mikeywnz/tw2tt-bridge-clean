from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.trade.domain.order import Order

import sys

try:
    # Read from command line
    symbol = sys.argv[1]
    action = sys.argv[2].lower()  # 'buy' or 'sell'
    quantity = int(sys.argv[3])

    # Setup Tiger Trade API
    config = TigerOpenClientConfig()  # Uses .properties file automatically
    client = TradeClient(config)

    # Create order object
    order = Order()
    order.symbol = symbol
    order.quantity = quantity
    order.action = action
    order.order_type = 'MKT'       # Market order
    order.sec_type = 'FUT'         # Futures
    order.currency = 'USD'
    order.exchange = 'CME'

    # Place order
    response = client.place_order(order)
    print("✅ Trade executed:", response)

except Exception as e:
    print(f"❌ Error occurred: {e}")