from datetime import datetime, timedelta, timezone
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    start_time_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
    end_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    try:
        inactive_orders = client.get_inactive_orders(
            account=account,
            symbol=symbol,
            sec_type=SegmentType.FUT,
            start_time=start_time_ms,
            end_time=end_time_ms,
            sort_by="LATEST_CREATED",
            limit=10
        )
        if inactive_orders is None:
            print("No inactive orders returned.")
            return

        print(f"Fetched {len(inactive_orders)} inactive orders:")
        for order in inactive_orders:
            print("Inactive Order:")
            for key, value in vars(order).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching inactive orders: {e}")

if __name__ == "__main__":
    main()