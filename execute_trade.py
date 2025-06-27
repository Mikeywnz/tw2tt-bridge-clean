from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

import sys

try:
    # Parse arguments from TradingView alert
    # Example: MGC2508aCME buy 1
    symbol = sys.argv[1]     # e.g. 'MGC2508aCME'
    action = sys.argv[2]     # 'buy' or 'sell'
    quantity = int(sys.argv[3])

    # ✅ Initialize config and client
    config = TigerOpenClientConfig()  # Uses .properties file automatically
    client = TradeClient(config)

    # ✅ Place market order using plain strings (no Enums)
    result = client.place_order(
        symbol=symbol,
        sec_type='FUT',
        exchange='CME',
        currency='USD',
        action=action.upper(),  # 'BUY' or 'SELL'
        order_type='MKT',
        quantity=quantity
    )

    print("✅ Trade executed:", result)

except Exception as e:
    print(f"❌ Error occurred: {e}")