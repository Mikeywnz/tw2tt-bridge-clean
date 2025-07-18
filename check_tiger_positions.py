from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

config = TigerOpenClientConfig()
client = TradeClient(config)

assets = client.get_assets()
print("✅ Account Assets:", assets)