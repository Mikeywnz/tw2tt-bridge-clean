from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    try:
        transactions = client.get_transactions(account=account, symbol="MGC2508", limit=10)
        for tx in transactions:
            print(vars(tx))
    except Exception as e:
        print(f"Error fetching transactions: {e}")

if __name__ == "__main__":
    main()