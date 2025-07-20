from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus

import firebase_admin
from firebase_admin import credentials, db
import time

# === Initialize Firebase ===
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

# === Setup Tiger API ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Get Open Positions ===
positions = client.get_positions(
    account="21807597867063647",
    sec_type=SegmentType.FUT
)

print("\n📊 Open Positions:")
if not positions:
    print("⚠️ No open futures positions.")
else:
    for p in positions:
        print(p)

        # === Parse position ===
        raw_contract = str(getattr(p, 'contract', ''))
        symbol = raw_contract.split('/')[0] if '/' in raw_contract else raw_contract
        quantity = getattr(p, 'quantity', 0)
        avg_cost = getattr(p, 'average_cost', 0.0)
        market_price = getattr(p, 'market_price', 0.0)
        timestamp = int(time.time() * 1000)

        payload = {
            "symbol": symbol,
            "quantity": quantity,
            "average_cost": avg_cost,
            "market_price": market_price,
            "timestamp": timestamp
        }

        try:
            ref = db.reference(f"/live_positions/{symbol}")
            ref.set(payload)
            print(f"✅ Pushed position for {symbol}")
        except Exception as e:
            print(f"❌ Failed to push position for {symbol}: {e}")