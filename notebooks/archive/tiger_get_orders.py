from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from datetime import datetime, timedelta

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    # Use date range last 7 days for example
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=7)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    try:
        orders = client.get_orders(account=account, symbol=symbol, start_date=start_str, end_date=end_str)
        for order in orders:
            print("Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching orders: {e}")

if __name__ == "__main__":
    main()