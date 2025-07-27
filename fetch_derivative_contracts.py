from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

def main():
    # Initialize TigerOpen client config and trade client
    config = TigerOpenClientConfig()
    client = TradeClient(config)

    # October 2025 Micro Gold futures contract symbol
    contract_symbol = "MGC2510"

    try:
        # Fetch detailed contract info
        contract_info = client.get_contract(contract_symbol)
        print("Contract info:")
        print(contract_info)
    except Exception as e:
        print(f"Error fetching contract info: {e}")

if __name__ == "__main__":
    main()