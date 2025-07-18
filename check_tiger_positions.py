from tigeropen.trade.trade_client import TradeClient
from tigeropen.tiger_open_config import TigerOpenClientConfig

config = TigerOpenClientConfig()
client = TradeClient(config)

positions = client.get_positions()
print("📊 Raw TigerTrade positions dict:")
print(positions)