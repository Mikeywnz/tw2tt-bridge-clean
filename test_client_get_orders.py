 
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

def dump_obj(o):
    for attr in sorted(a for a in dir(o) if not a.startswith("_")):
        try:
            val = getattr(o, attr)
            if callable(val): 
                continue
            print(f"{attr}: {val}")
        except Exception as e:
            print(f"{attr}: <error: {e}>")

def test_get_orders():
    client = TradeClient(TigerOpenClientConfig())
    account_id = "21807597867063647"
    symbol = "MGC2510"
    orders = client.get_orders(account=account_id, seg_type=SegmentType.FUT, symbol=symbol, limit=10)
    print(f"Fetched {len(orders)} orders for symbol {symbol}:")
    for i, order in enumerate(orders, 1):
        print(f"\n--- Order #{i} RAW DUMP ---")
        dump_obj(order)

if __name__ == "__main__":
    test_get_orders()

    
# from tigeropen.tiger_open_config import TigerOpenClientConfig
# from tigeropen.trade.trade_client import TradeClient
# from tigeropen.common.consts import SegmentType
# from datetime import datetime

# def test_get_orders():
#     # Initialize client config and client
#     config = TigerOpenClientConfig()
#     client = TradeClient(config)

#     # Define account and symbol to fetch orders for
#     account_id = "21807597867063647"
#     symbol = "MGC2510"  # Adjust as needed

#     # Fetch orders from API
#     orders = client.get_orders(
#         account=account_id,
#         seg_type=SegmentType.FUT,
#         symbol=symbol,
#         limit=10
#     )

#     print(f"Fetched {len(orders)} orders for symbol {symbol}:")

#     # Print details of each order
#     for order in orders:
#         print(f"Order ID: {getattr(order, 'order_id', None)}")
#         print(f"  Status: {getattr(order, 'status', None)}")
#         print(f"  Filled Price: {getattr(order, 'filled_price', None)}")
#         print(f"  Filled Quantity: {getattr(order, 'filled_quantity', None)}")
#         print(f"  Action: {getattr(order, 'action', None)}")
#         print(f"  Contracts Remaining: {getattr(order, 'contracts_remaining', None)}")
#         print(f"  Transaction Time: {getattr(order, 'transaction_time', None)}")
#         print("---")

# if __name__ == "__main__":
#     test_get_orders()