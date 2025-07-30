from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    try:
        positions = client.get_positions(account=account, symbol=symbol)
        for pos in positions:
            print("Position:")
            for key, value in vars(pos).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching positions: {e}")

if __name__ == "__main__":
    main()