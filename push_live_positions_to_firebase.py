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
import firebase_active_contract
from datetime import date
import rollover_updater  # Your rollover script filename without .py
import firebase_admin
from datetime import timezone
import pytz


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
def fetch_trade_transactions(account_id):
    try:
        active_symbol = firebase_active_contract.get_active_contract()
        if not active_symbol:
            print("❌ No active contract symbol found in Firebase; aborting fetch_trade_transactions")
            return []
        transactions = client.get_transactions(account=account_id, symbol=active_symbol, limit=20)
        return transactions or []
    except Exception as e:
        print(f"❌ Failed to fetch trade transactions: {e}")
        return []



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

            # --- Update position count ---
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
            position_count = sum(getattr(pos, "quantity", 0) for pos in positions)
            timestamp_iso = datetime.now(timezone.utc).isoformat()

            now_nz = datetime.now(pytz.timezone("Pacific/Auckland"))
            timestamp_readable = now_nz.strftime("%Y-%m-%d %H:%M:%S NZST")

            live_ref.update({
                "position_count": position_count,
                "last_updated": timestamp_readable
            })
            print(f"✅ Pushed position count {position_count} at {timestamp_iso}")

            # --- Fetch trade activities for accurate prices ---
            transactions = fetch_trade_transactions(account_id="21807597867063647")

            print(f"🔍 Fetched {len(transactions)} transactions")
            for tx in transactions:
                print(vars(tx))

            # --- Keep /live_total_positions/ path alive ---
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(5)  # Pause 5 seconds before next update


if __name__ == "__main__":
    push_live_positions()

#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE (END OF SCRIPT)  ================================