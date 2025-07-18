from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

# ✅ Safe: config auto-loads from tiger_openapi_config.properties
config = TigerOpenClientConfig()
client = TradeClient(config)

# 🔍 Get open positions
positions = client.get_positions()
print("📊 Current TigerTrade positions:")
for pos in positions:
    print(pos)