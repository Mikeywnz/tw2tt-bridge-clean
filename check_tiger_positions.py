from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

config = TigerOpenClientConfig()
client = TradeClient(config)

orders = client.get_orders()
print("📋 Recent TigerTrade orders:")
for o in orders:
    print(o)