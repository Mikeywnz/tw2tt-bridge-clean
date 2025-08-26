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


# === Helper: Fetch Trade Transactions from Tiger (for accurate entry prices) ===
# def fetch_trade_transactions(account_id):
#     try:
#         active_symbol = firebase_active_contract.get_active_contract()
#         if not active_symbol:
#             print("❌ No active contract symbol found in Firebase; aborting fetch_trade_transactions")
#             return []
#         transactions = client.get_transactions(account=account_id, symbol=active_symbol, limit=10)
#         return transactions or []
#     except Exception as e:
#         print(f"❌ Failed to fetch trade transactions: {e}")
#         return []



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

            # --- Collect positions (Futures + Stocks) ---
            all_positions = []
            for sec_type in [SegmentType.FUT, SegmentType.STK]:
                try:
                    pos = client.get_positions(account="21807597867063647", sec_type=sec_type) or []
                    all_positions.extend(pos)
                except Exception as e:
                    print(f"⚠️ Failed to fetch {sec_type}: {e}")

            # --- Push each position into Firebase ---
            updates = {}
            for pos in all_positions:
                symbol = getattr(pos, "symbol", None)
                qty    = getattr(pos, "quantity", 0)
                avg_px = getattr(pos, "avg_price", 0.0)
                sec    = getattr(pos, "sec_type", "UNKNOWN")
                if not symbol:
                    continue
                updates[symbol] = {
                    "quantity": qty,
                    "avg_price": avg_px,
                    "sec_type": str(sec)
                }

            # Always include timestamp
            now_nz_dt = datetime.now(pytz.timezone("Pacific/Auckland"))
            updates["last_updated"] = now_nz_dt.strftime("%Y-%m-%d %H:%M:%S NZST")

            live_ref.set(updates)
            print(f"✅ Pushed {len(all_positions)} positions at {updates['last_updated']}")

            # --- Keep /live_total_positions/ path alive if empty ---
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(20)  # Pause 20 seconds before next update