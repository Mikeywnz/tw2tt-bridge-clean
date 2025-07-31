from datetime import datetime, timedelta
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    # Timestamp for 1 minute ago, in milliseconds
    start_time_ms = int((datetime.utcnow() - timedelta(minutes=1)).timestamp() * 1000)

    try:
        # Fetch open orders with start_time filter (no limit)
        open_orders = client.get_open_orders(account=account, symbol=symbol, start_time=start_time_ms)

        if not open_orders:
            print("No open orders returned.")
            return

        print(f"Fetched {len(open_orders)} open orders since 1 minute ago:")
        for order in open_orders:
            print("Open Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == "__main__":
    main()