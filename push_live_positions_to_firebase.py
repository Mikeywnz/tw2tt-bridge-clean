# =========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  =========================

import os
import time
from datetime import datetime
import pytz

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

import rollover_updater  # Your rollover script filename without .py

import firebase_admin
from firebase_admin import credentials, initialize_app, db
import firebase_active_contract  # (kept; not used here but likely used by your env)

# === Firebase Key ===
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"

# === Firebase Initialization ===
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(firebase_key_path)
        initialize_app(cred, {
            'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
        })
except Exception as e:
    print(f"❌ Firebase init failed: {e}", flush=True)
    # Don't exit; Render would restart us. Keep running so we can try again next loop.

# === TigerOpen Client Setup ===
config = TigerOpenClientConfig()
client = TradeClient(config)

NZ_TZ = pytz.timezone("Pacific/Auckland")

# === 🟩 DAILY ROLLOVER UPDATER INTEGRATION + LIVE POSITIONS PUSH 🟩 ===
def push_live_positions():
    live_ref = db.reference("/live_total_positions")

    last_rollover_date = None
    print("🟢 push_live_positions worker started", flush=True)

    while True:
        try:
            # --- daily rollover check (NZ date boundary) ---
            now_nz_date = datetime.now(NZ_TZ).date()
            if last_rollover_date != now_nz_date:
                try:
                    print(f"⏰ Running daily rollover check for {now_nz_date}", flush=True)
                    rollover_updater.main()  # Call your rollover script
                except Exception as re:
                    print(f"⚠️ Rollover updater failed: {re}", flush=True)
                last_rollover_date = now_nz_date

            # --- Collect positions (Futures + Stocks) ---
            all_positions = []
            for sec_type in (SegmentType.FUT, SegmentType.STK):
                try:
                    pos = client.get_positions(account="21807597867063647", sec_type=sec_type) or []
                    all_positions.extend(pos)
                except Exception as e:
                    print(f"⚠️ Failed to fetch {sec_type}: {e}", flush=True)

            # --- Build payload ---
            updates = {}
            for pos in all_positions:
                symbol = getattr(pos, "symbol", None)
                qty    = getattr(pos, "quantity", 0)
                avg_px = getattr(pos, "avg_price", 0.0)
                sec    = getattr(pos, "sec_type", "UNKNOWN")
                if not symbol:
                    continue
                updates[symbol] = {
                    "quantity": int(qty) if isinstance(qty, (int, float)) else 0,
                    "avg_price": float(avg_px) if isinstance(avg_px, (int, float)) else 0.0,
                    "sec_type": str(sec)
                }

            # Timestamp with correct NZ zone abbreviation
            now_nz = datetime.now(NZ_TZ)
            updates["last_updated"] = now_nz.strftime("%Y-%m-%d %H:%M:%S ") + now_nz.tzname()

            # --- Push to Firebase ---
            live_ref.set(updates)
            print(f"✅ Pushed {len(all_positions)} positions at {updates['last_updated']}", flush=True)

            # --- Keep path alive if somehow empty ---
            snap = live_ref.get()
            if not snap:
                live_ref.child("_heartbeat").set("alive")
        except Exception as e:
            print(f"❌ Error pushing live positions: {e}", flush=True)

        time.sleep(20)  # Pause 20 seconds before next update


if __name__ == "__main__":
    # 🚀 Start the long-running loop so Render doesn't see "Application exited early"
    push_live_positions()