import sys
import os
import json
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.trade.domain.contract import Contract
from tigeropen.trade.domain.order import Order

def place_trade(symbol, action, quantity):
    symbol = symbol.upper()
    action = action.upper()
    print(f"📂 Executing Trade → Symbol: {symbol}, Action: {action}")

    # === Load Tiger Config ===
    try:
        config = TigerOpenClientConfig()
        config.env = 'PROD'
        config.language = 'en_US'

        if not config.account:
            raise ValueError("Tiger config loaded but account is missing or blank.")

        client = TradeClient(config)

    except Exception as e:
        print(f"❌ Failed to load Tiger API config or initialize client: {e}")
        raise e

    # 🔒 === LOCKED: Define Futures Contract (do not modify this block) ===
    contract = Contract()
    contract.symbol = symbol
    contract.sec_type = 'FUT'
    contract.currency = 'USD'
    contract.exchange = 'CME'

    # 🔒 === LOCKED: Create Order (exact format TigerTrade requires) ===
    order = Order(
        account=config.account,
        contract=contract,
        action=action
    )
    order.order_type = 'MKT'  # 🔒 Must be 'MKT' — Tiger's required market order code
    order.quantity = quantity

    # === Submit Order ===
    try:
        response = client.place_order(order)
        print("✅ ORDER PLACED")
        print("✅ Order submitted. Raw Response:", response)
        print("🐯 Full Tiger Response Dict:", response.__dict__)

        error_msg = getattr(response, "error_msg", "No error_msg")
        print("❗Tiger response message:", error_msg)

    except Exception as e:
        print("❌ Tiger API Exception raised:")
        print(e)
        if hasattr(e, 'args') and len(e.args) > 0:
            print("🧪 Tiger error details:", e.args[0])
        import traceback
        traceback.print_exc()
        raise e

    # === Check Fill Status ===
    if response:
        order_status = getattr(response, "status", "").upper()
        filled_qty = getattr(response, "filled", 0)
        is_filled = order_status == "FILLED" or filled_qty > 0

        # Get Live Price from local file
        live_price = 0.0
        try:
            with open(os.path.join(os.path.dirname(__file__), 'live_prices.json')) as f:
                live_data = json.load(f)
                data = live_data.get(symbol)
                if isinstance(data, dict):
                    live_price = float(data.get("price", 0.0))
                elif isinstance(data, (float, int)):
                    live_price = float(data)
        except Exception as e:
            print("⚠️ Could not read live_prices.json:", e)

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if is_filled:
            print(f"✅ Trade confirmed filled at approx. ${live_price} 🕒 timestamp {timestamp}", flush=True)
            print("SUCCESS", flush=True)
            print("=== DEBUG: SUCCESS line printed and flushed ===", flush=True)
            return "SUCCESS"
        else:
            print("⚠️ Order not filled – no further logging will occur.")
            return "NOT_FILLED"
    else:
        print("❌ No valid response received from TigerTrade. Cannot confirm trade status.")
        return "NO_RESPONSE"

# Optional CLI support
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 execute_trade_live.py <symbol> <buy/sell>")
        sys.exit(1)
    symbol = sys.argv[1]
    action = sys.argv[2]
    quantity = 1
    place_trade(symbol, action, quantity)