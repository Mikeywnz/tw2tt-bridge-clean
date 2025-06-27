# execute_trade.py – Clean working version, minimal and enum-free

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.trade.request import Order

import sys

try:
    # Parse alert payload like: 'MGC2508aCME buy 1'
    symbol, action, quantity = sys.argv[1], sys.argv[2].lower(), int(sys.argv[3])

    # Load config from .properties file automatically
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    # Build order using plain strings
    order = Order(
        symbol=symbol,
        sec_type='FUT',              # plain string, not SecType.FUT
        currency='USD',              # plain string
        exchange='CME',              # plain string
        action=action.upper(),       # 'BUY' or 'SELL'
        order_type='MKT',            # market order
        quantity=quantity
    )

    # Place the order
    response = client.place_order(order)
    print("✅ Trade sent:", response)

except Exception as e:
    print(f"❌ Trade execution failed: {e}")