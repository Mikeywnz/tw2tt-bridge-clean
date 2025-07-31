from datetime import datetime, timedelta
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    # 24 hours ago in milliseconds UTC
    start_time_ms = int((datetime.utcnow() - timedelta(hours=24)).timestamp() * 1000)

    try:
        open_orders = client.get_open_orders(account=account, symbol=symbol, start_time=start_time_ms)
        if open_orders is None:
            print("No open orders returned.")
            return

        print(f"Fetched {len(open_orders)} open orders since 24 hours ago:")
        for order in open_orders:
            print("Open Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == "__main__":
    main()