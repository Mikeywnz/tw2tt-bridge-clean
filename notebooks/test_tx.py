from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    try:
        transactions = client.get_transactions(account=account, symbol=symbol, limit=20)
        for tx in transactions:
            print("Transaction:")
            for key, value in vars(tx).items():
                print(f"  {key}: {value}")
            print()
    except Exception as e:
        print(f"Error fetching transactions: {e}")

if __name__ == "__main__":
    main()