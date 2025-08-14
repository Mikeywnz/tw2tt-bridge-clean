from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def print_items(label, items):
    print(f"\n=== {label} ({len(items) if items else 0} items) ===\n")
    if not items:
        print("No data")
        return
    for idx, item in enumerate(items):
        print(f"{label} #{idx+1}:")
        for key, value in vars(item).items():
            print(f"  {key}: {value}")
        print()

def main():
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    account = "21807597867063647"
    symbol = "MGC2510"

    try:
        positions = client.get_positions(account=account, symbol=symbol)
        print_items("Positions", positions)
    except Exception as e:
        print(f"Error fetching positions: {e}")

    try:
        open_orders = client.get_open_orders(account=account, symbol=symbol)
        print_items("Open Orders", open_orders)
    except Exception as e:
        print(f"Error fetching open orders: {e}")

    try:
        orders = client.get_orders(account=account, symbol=symbol)
        print_items("Orders", orders)
    except Exception as e:
        print(f"Error fetching orders: {e}")

    try:
        filled_orders = client.get_filled_orders(account=account, symbol=symbol)
        print_items("Filled Orders", filled_orders)
    except Exception as e:
        print(f"Error fetching filled orders: {e}")

    try:
        transactions = client.get_transactions(account=account, symbol=symbol, limit=10)
        print_items("Transactions", transactions)
    except Exception as e:
        print(f"Error fetching transactions: {e}")

if __name__ == "__main__":
    main()