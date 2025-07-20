import firebase_admin
from firebase_admin import credentials, db
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

# --- Initialize Firebase
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

# --- Tiger client setup (default config method)
config = TigerOpenClientConfig()
client = TradeClient(config)
positions = client.get_positions()
tiger_symbols = [pos.symbol for pos in positions if pos.quantity != 0]

# --- Pull all Firebase orders
ref = db.reference("/order_status")
orders = ref.get() or {}

updates = 0

for order_id, order_data in orders.items():
    symbol = order_data.get("symbol")
    is_open = order_data.get("is_open", False)
    
    if is_open:
        # GHOST DETECTED: symbol is not in Tiger positions
        if symbol not in tiger_symbols:
            print(f"🔍 Marking ghost order {order_id} ({symbol}) as CLOSED...")
            update_fields = {
                "is_open": False,
                "exit_reason": "GHOST",
                "status": "ORDERSTATUS.UNKNOWN"
            }

            # Optional: fix Chinese reason
            reason = order_data.get("reason", "")
            if "可用资金" in reason or "不足" in reason:
                update_fields["reason"] = "LACK_OF_MARGIN"

            ref.child(order_id).update(update_fields)
            updates += 1

print(f"✅ Reconciliation complete. Orders updated: {updates}")