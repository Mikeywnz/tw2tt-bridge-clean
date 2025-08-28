#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  ================================

import time
from datetime import datetime, timezone, date
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
import rollover_updater  # Your rollover script filename without .py
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
            now_nz_date = datetime.now(pytz.timezone("Pacific/Auckland")).date()
            if last_rollover_date != now_nz_date:
                print(f"⏰ Running daily rollover check for {now_nz_date}")
                rollover_updater.main()  # Call rollover script main function
                last_rollover_date = now_nz_date

            # --- Update per-symbol NET positions (signed; e.g., -3 short, +2 long, 0 flat) ---
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)

            by_symbol = {}
            for pos in (positions or []):
                # Tiger often returns 'contract' like 'MES2509/FUT/USD/None' — take the symbol before the first '/'
                contract_or_sym = str(getattr(pos, "contract", getattr(pos, "symbol", "")) or "")
                sym = contract_or_sym.split("/", 1)[0].strip()

                # quantity should already be signed (+ long / - short) from Tiger
                qty = getattr(pos, "quantity", getattr(pos, "position_qty", 0)) or 0
                try:
                    qty = int(qty)
                except Exception:
                    try:
                        qty = int(float(qty))
                    except Exception:
                        qty = 0

                if not sym or qty == 0:
                    # keep exact mirror: if Tiger shows 0 net, omit/leave 0
                    by_symbol.setdefault(sym or "UNKNOWN", 0)
                    continue

                by_symbol[sym] = by_symbol.get(sym, 0) + qty

            # Timestamps
            now_nz = datetime.now(pytz.timezone("Pacific/Auckland"))
            timestamp_readable = now_nz.strftime("%Y-%m-%d %H:%M:%S NZST")

            # Write ONLY the per-symbol net map + timestamp (no global position_count)
            live_ref.update({
                "by_symbol": by_symbol,           # e.g. {"MGC2510": -3, "MES2509": 1}
                "last_updated": timestamp_readable
            })
            print(f"✅ Pushed by_symbol={by_symbol}")

            # --- Keep /live_total_positions/ path alive (legacy) ---
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(20)  # Pause 20 seconds before next update


if __name__ == "__main__":
    push_live_positions()

#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE (END OF SCRIPT)  ================================