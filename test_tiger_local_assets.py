from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

# ✅ Load config (must have tiger_openapi_config.properties in working dir)
config = TigerOpenClientConfig()
client = TradeClient(config)

# 🔍 Get account assets
assets = client.get_assets()

print("✅ Local TigerTrade asset fetch succeeded:")
print(assets)