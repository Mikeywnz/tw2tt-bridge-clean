from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

config = TigerOpenClientConfig()
client = TradeClient(config)

# List all methods and attributes available on client
for item in dir(client):
    print(item)