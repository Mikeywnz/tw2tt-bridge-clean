#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  ================================

import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from datetime import datetime
from pytz import timezone
from datetime import date
import rollover_updater  # Your rollover script filename without .py
from datetime import timezone
import pytz
from firebase_admin import credentials, initialize_app, db
import firebase_admin
import firebase_active_contract
import os

# === Firebase Key ===
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# === Firebase Initialization ===
if not firebase_admin._apps:
    firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
    cred = credentials.Certificate(firebase_key_path)
    initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })


# === TigerOpen Client Setup ===
config = TigerOpenClientConfig()
client = TradeClient(config)


# === 🟩 DAILY ROLLOVER UPDATER INTEGRATION 🟩 ===
def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    open_trades_ref = db.reference("/open_active_trades")

    last_rollover_date = None

    while True:
        try:
            now_nz = datetime.now(pytz.timezone("Pacific/Auckland")).date()
            if last_rollover_date != now_nz:
                print(f"⏰ Running daily rollover check for {now_nz}")
                rollover_updater.main()  # Call rollover script main function
                last_rollover_date = now_nz

            # --- Update position count (plus per-symbol map) ---
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
            by_symbol = {}
            for pos in (positions or []):
                # Tiger returns 'contract' like 'MES2509/FUT/USD/None' — take the symbol before the first '/'
                sym = str(getattr(pos, "contract", getattr(pos, "symbol", ""))).split("/", 1)[0]
                qty = getattr(pos, "quantity", getattr(pos, "position_qty", 0)) or 0
                try:
                    qty = abs(int(qty))
                except Exception:
                    try:
                        qty = abs(int(float(qty)))
                    except Exception:
                        qty = 0
                if not sym or qty == 0:
                    continue
                by_symbol[sym] = by_symbol.get(sym, 0) + qty

            position_count = sum(by_symbol.values())
            timestamp_iso = datetime.now(timezone.utc).isoformat()

            #============Firebase write=========================================== 
            now_nz = datetime.now(pytz.timezone("Pacific/Auckland"))
            timestamp_readable = now_nz.strftime("%Y-%m-%d %H:%M:%S NZST")
            
            live_ref.update({
                    "position_count": position_count,
                    "by_symbol": by_symbol,   # NEW: {"MES2509": 1, "MGC2510": 1}
                    "last_updated": timestamp_readable
            })
            print(f"✅ Pushed position_count={position_count}, by_symbol={by_symbol}")

            # --- Keep /live_total_positions/ path alive ---
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(20)  # Pause 20 seconds before next update


if __name__ == "__main__":
    push_live_positions()

#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE (END OF SCRIPT)  ================================