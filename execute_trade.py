from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import Market, Currency, OrderType

import sys

try:
    # Get command line args: ticker, side (buy/sell), quantity
    ticker = sys.argv[1]
    side = sys.argv[2].lower()
    quantity = int(sys.argv[3])

    # Set up TigerOpen client
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    # Build order
    contract = {
        'symbol': ticker,
        'sec_type': SecType.FUTURE,
        'exchange': 'CME',
        'currency': Currency.USD
    }

    order = {
        'action': 'BUY' if side == 'buy' else 'SELL',
        'order_type': OrderType.MARKET,
        'quantity': quantity
    }

    # Place order
    result = client.place_order(contract, order)
    print(f"✅ Order result: {result}")

except Exception as e:
    print(f"❌ Error occurred: {e}")