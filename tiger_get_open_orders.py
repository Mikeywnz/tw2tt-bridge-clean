from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from datetime import datetime, timedelta

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    # Define date range: last 7 days
    start_date = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = datetime.utcnow().strftime("%Y-%m-%d")

    try:
        open_orders = client.get_open_orders(account=account, symbol=symbol, start_date=start_date, end_date=end_date)
        for order in open_orders:
            print("Open Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == "__main__":
    main()