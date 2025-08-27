from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"

    try:
        positions = client.get_positions(account=account, sec_type=SegmentType.FUT)
        if not positions:
            print("No open positions found.")
        else:
            print(f"Fetched {len(positions)} positions:")
            for pos in positions:
                print(vars(pos))
    except Exception as e:
        print(f"Error fetching positions: {e}")

if __name__ == "__main__":
    main()