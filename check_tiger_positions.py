from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

config = TigerOpenClientConfig()
client = TradeClient(config)

try:
    positions = client.get_positions()
    print("🔍 Raw output from get_positions():")
    print(type(positions))
    print(positions)

    if not positions:
        print("⚠️ No positions returned. Possible demo account limitation.")
    else:
        for p in positions:
            print("▶️", p)

except Exception as e:
    print(f"❌ Error calling get_positions(): {e}")