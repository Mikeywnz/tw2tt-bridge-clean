from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from datetime import datetime, timedelta, timezone

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    # Timezone-aware dates for potential future use
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    try:
        # get_open_orders does NOT accept start_date/end_date; removed here
        open_orders = client.get_open_orders(account=account, symbol=symbol)
        if not open_orders:
            print("No open orders found.")
        for order in open_orders:
            print("Open Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == "__main__":
    main()