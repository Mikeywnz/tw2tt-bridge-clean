from datetime import datetime, timedelta
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"

    # 24 hours ago in milliseconds UTC
    start_time_ms = int((datetime.utcnow() - timedelta(hours=24)).timestamp() * 1000)

    try:
        open_orders = client.get_open_orders(account=account, sec_type=SegmentType.FUT, start_time=start_time_ms)
        if not open_orders:
            print("No open orders found.")
        else:
            print(f"Fetched {len(open_orders)} open orders since 24 hours ago:")
            for order in open_orders:
                print(vars(order))
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == "__main__":
    main()