import sys
print("🚀 execute_trade.py started")

from tigeropenapi.tiger_open_config import TigerOpenClientConfig
from tigeropen.open_context import OpenContext
from tigeropen.common.consts import Language
from tigeropen.trade.domain.order import Order
from tigeropen.trade.request import OrderType, Action

# 🧾 Step 1: Get arguments
ticker = sys.argv[1]
side = sys.argv[2].lower()
qty = int(sys.argv[3])

# 🧾 Step 2: Load Tiger API config
config = TigerOpenClientConfig.from_config_file('tiger_openapi_config.properties')
open_context = OpenContext(config)
open_context.set_language(Language.en_US)
open_context.open()

# 🧾 Step 3: Build order
client = open_context.get_trade_client()
action = Action.BUY if side == 'buy' else Action.SELL

order = Order(
    account=config.paper_account,        # Uses your demo account
    contract_code=ticker,                # e.g. 'MES1!'
    action=action,                       # BUY or SELL
    order_type=OrderType.MARKET,         # Market order
    quantity=qty,
    sec_type='FUTURE'                    # Micro futures
)

# 🧾 Step 4: Submit order
response = client.place_order(order)
print(f"✅ TigerTrade response: {response}")