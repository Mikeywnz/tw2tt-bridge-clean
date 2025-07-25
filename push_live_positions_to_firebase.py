import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.const import SegmentType
from firebase_admin import credentials, initialize_app, db

# Initialize Firebase Admin SDK (adjust path to your credentials JSON)
cred = credentials.Certificate("service_account.json")
initialize_app(cred, {
    'databaseURL': 'https://your-firebase-db-url.firebaseio.com'
})

# Setup TigerOpen client exactly as your existing code
config = TigerOpenClientConfig()
client = TradeClient(config)

def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    while True:
        try:
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
            position_count = len(positions)
            timestamp_iso = datetime.utcnow().isoformat() + 'Z'

            live_ref.update({
                "position_count": position_count,
                "last_updated": timestamp_iso
            })
            print(f"✅ Pushed position count {position_count} at {timestamp_iso}")

            # Keep /live_total_positions/ alive
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(30)  # Wait 30 seconds before next update

if __name__ == "__main__":
    push_live_positions()