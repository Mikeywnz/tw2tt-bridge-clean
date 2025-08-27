# =========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  =========================
import os
import time
from datetime import datetime
import pytz

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

import rollover_updater  # your rollover script, callable via .main()

import firebase_admin
from firebase_admin import credentials, initialize_app, db

# --- Firebase init ---
FIREBASE_KEY_PATH = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
DB_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        initialize_app(cred, {"databaseURL": DB_URL})
except Exception as e:
    print(f"❌ Firebase init failed: {e}", flush=True)

# --- Tiger client ---
config = TigerOpenClientConfig()
client = TradeClient(config)

# --- Timezone ---
NZ_TZ = pytz.timezone("Pacific/Auckland")

# --- Your Tiger account id (unchanged) ---
ACCOUNT_ID = "21807597867063647"

def _fetch_positions_safe(sec_type):
    """Return a list of position objects for the given security type, or []."""
    try:
        pos = client.get_positions(account=ACCOUNT_ID, sec_type=sec_type) or []
        return pos
    except Exception as e:
        print(f"⚠️ get_positions failed for {sec_type}: {e}", flush=True)
        return []

def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    last_rollover_date = None
    print("🟢 push_live_positions worker started", flush=True)

    while True:
        try:
            # ---- Daily rollover check (NZ date) ----
            today_nz = datetime.now(NZ_TZ).date()
            if last_rollover_date != today_nz:
                try:
                    print(f"⏰ Running daily rollover for {today_nz}", flush=True)
                    rollover_updater.main()
                except Exception as re:
                    print(f"⚠️ Rollover updater failed: {re}", flush=True)
                last_rollover_date = today_nz

            # ---- Collect positions (Futures + Stocks) ----
            fut_positions = _fetch_positions_safe(SegmentType.FUT)
            stk_positions = _fetch_positions_safe(SegmentType.STK)

            all_positions = []
            all_positions.extend(fut_positions)
            all_positions.extend(stk_positions)

            print(f"📦 Fetched FUT={len(fut_positions)} STK={len(stk_positions)} (total={len(all_positions)})", flush=True)

            # ---- Build per-symbol payload ----
            updates = {}
            for pos in all_positions:
                sym = getattr(pos, "symbol", None)
                qty = getattr(pos, "quantity", 0)
                avg_px = getattr(pos, "avg_price", 0.0)
                sec = getattr(pos, "sec_type", "UNKNOWN")
                if not sym:
                    continue
                updates[sym] = {
                    "quantity": int(qty) if isinstance(qty, (int, float)) else 0,
                    "avg_price": float(avg_px) if isinstance(avg_px, (int, float)) else 0.0,
                    "sec_type": str(sec),
                }

            # ---- Aggregate totals + legacy field ----
            total_contracts = 0
            per_symbol_counts = {}
            for sym, info in updates.items():
                q = int(info.get("quantity", 0))
                per_symbol_counts[sym] = q
                total_contracts += q

            now_nz = datetime.now(NZ_TZ)
            timestamp_str = now_nz.strftime("%Y-%m-%d %H:%M:%S ") + now_nz.tzname()

            payload = {
                **updates,                        # per-symbol nodes
                "position_count": total_contracts, # ✅ legacy field (monitor/zombie uses this)
                "per_symbol_counts": per_symbol_counts,  # ✅ new: symbol breakdown
                "last_updated": timestamp_str,
            }

            # ---- Write to Firebase ----
            live_ref.set(payload)
            print(f"✅ Pushed position_count={total_contracts} across {len(per_symbol_counts)} symbols at {timestamp_str}", flush=True)

            # ---- Keep path alive (paranoia) ----
            snap = live_ref.get()
            if not snap:
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error in push_live_positions loop: {e}", flush=True)

        time.sleep(20)  # 20s cadence

if __name__ == "__main__":
    push_live_positions()
# =========================  END  =========================