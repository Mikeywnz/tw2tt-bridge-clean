#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  ================================
import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from firebase_admin import credentials, initialize_app, db
from datetime import datetime
from pytz import timezone
import os

firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# Initialize Firebase Admin SDK (adjust path to your credentials JSON)
initialize_app(cred, {
    'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
})

# Setup TigerOpen client exactly as your existing code
config = TigerOpenClientConfig()
client = TradeClient(config)


def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    while True:
        try:
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
            position_count = sum(getattr(pos, "quantity", 0) for pos in positions)
            timestamp_iso = datetime.utcnow().isoformat() + 'Z'

            now_nz = datetime.now(timezone("Pacific/Auckland"))
            timestamp_readable = now_nz.strftime("%Y-%m-%d %H:%M:%S NZST")

            live_ref.update({
                "position_count": position_count,
                "last_updated": timestamp_readable
            })
            print(f"✅ Pushed position count {position_count} at {timestamp_iso}")

            # Keep /live_total_positions/ alive
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(5)  # Wait 5 seconds before next update

if __name__ == "__main__":
    push_live_positions()

    #=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE (END OF SCRIPT)  ================================