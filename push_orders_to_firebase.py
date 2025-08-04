#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
import firebase_admin
from firebase_admin import credentials, db
import random
import string
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import csv
from pytz import timezone
import requests
import json
import firebase_active_contract
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import os
# 🟩 GREEN PATCH START: Grace period cache for zero contracts trades
import time

grace_cache = {}
# 🟩 GREEN PATCH END

FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

# Load Firebase secret key
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# === Firebase Init ===
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
    })

# === Load Trailing Take Profit Settings from Firebase ===
def load_trailing_tp_settings():
    try:
        fb_url = f"{FIREBASE_URL}/trailing_tp_settings.json"
        res = requests.get(fb_url)
        cfg = res.json() if res.ok else {}
        if cfg.get("enabled", False):
            return float(cfg.get("trigger_points", 14.0)), float(cfg.get("offset_points", 5.0))
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0

archived_trades_ref = db.reference("/archived_trades_log")
archived_trade_ids = set(archived_trades_ref.get() or {})
    
# === Setup Tiger API ===
config = TigerOpenClientConfig()
client = TradeClient(config)

#logged_ghost_ids_ref = db.reference("/logged_ghost_ids")
#logged_ghost_order_ids = set(logged_ghost_ids_ref.get() or [])

# === Google Sheets Setup (Global) ===
from google.oauth2.service_account import Credentials
import gspread

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
CLOSED_TRADES_FILE = "closed_trades.csv"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open("Closed Trades Journal").worksheet("journal")
    return sheet

    # === Helper to Check if Trade ID is a Known Ghost Trade ===
# ==========================
# 🟩 GREEN PATCH: Uncomment and fix is_ghost_trade helper function
# ==========================
def is_ghost_trade(trade_id, firebase_db):
    ghost_ref = firebase_db.reference("/ghost_trades_log")
    ghosts = ghost_ref.get() or {}
    return trade_id in ghosts

# === STEP 3A: Helper to Check if Trade ID is a Known Zombie ===
def is_zombie_trade(trade_id, firebase_db):
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombies = zombie_ref.get() or {}
    return trade_id in zombies

def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

    # ===== Archived_trade() helper function =====
def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{trade_id}")  # <-- changed here
        # Preserve original trade_type; do NOT overwrite it with "closed"
        if "trade_type" not in trade or not trade["trade_type"]:
            trade["trade_type"] = "UNKNOWN"
        archive_ref.set(trade)
        print(f"✅ Archived trade {trade_id}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {trade_id}: {e}")
        return False


# === Manual Flatten Block Helper MAY BE OUTDATED AND REDUNDANT NOW ===
def safe_float(val):
    try:
        return float(val)
    except:
        return 0.0

def map_source(raw_source):
    if raw_source is None:
        return "unknown"
    lower = raw_source.lower()
    if "openapi" in lower:
        return "OpGo"
    elif "desktop" in lower:
        return "Tiger Desktop"
    elif "mobile" in lower:
        return "tiger-mobile"
    return "unknown"

def get_exit_reason(status, reason, filled):
    if status == "CANCELLED" and filled == 0:
        return "CANCELLED"
    elif status == "EXPIRED" and filled == 0 and reason and ("资金" in reason or "margin" in reason.lower()):
        return "LACK_OF_MARGIN"
    elif "liquidation" in reason.lower():
        return "liquidation"
    elif status == "FILLED":
        return "FILLED"
    return status

#=====  END OF PART 1 =====

#=========================  PUSH_ORDERS_TO_FIREBASE - PART 2  ================================

# === Helper: Safe int cast for sorting entry_timestamps ===
def safe_int(value):
    try:
        return int(value)
    except:
        return 0

# === MAIN FUNCTION WRAPPED HERE ===
def push_orders_main():

    tiger_orders_ref = db.reference("/ghost_trades_log")  # rename from tiger_orders_log
    open_trades_ref = db.reference("open_active_trades")
    pos_tracker = {}
    now = datetime.utcnow()
    #start_time = now - timedelta(hours=48)
    #end_time = now

    # Initialize counters here, BEFORE the order loop:
    filled_count = 0
    cancelled_count = 0
    lack_margin_count = 0
    unknown_count = 0

    REASON_MAP = {
        "trailing_tp_exit": "Trailing Take Profit",
        "manual_close": "Manual Close",
        "ema_flattening_exit": "EMA Flattening",
        "liquidation": "Liquidation",
        "LACK_OF_MARGIN": "Lack of Margin",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Lack of Margin",
        # Add raw Tiger strings mapped here if needed
    }

   # =================== GREEN PATCH: Use Active Contract Symbol ===================

    # Before fetching orders from TigerTrade API, fetch the active contract from Firebase
    active_symbol = firebase_active_contract.get_active_contract()
    if not active_symbol:
        print("❌ No active contract symbol found in Firebase; aborting orders fetch")
        return  # Or raise Exception or handle error as appropriate

    # Then pass active_symbol as a filter parameter to client.get_orders()
    orders = client.get_orders(
        account="21807597867063647",
        seg_type=SegmentType.FUT,
        symbol=active_symbol,  # if you added this from patch
        limit=30
    )

    print(f"\n📦 Total orders returned for active contract {active_symbol}: {len(orders)}")

    # ====================== GREEN PATCH START: Push Orders Processing Fixes ======================
    # 🟩 GREEN PATCH START: Refresh archived trades cache inside main loop
    archived_trade_ids = set(archived_trades_ref.get() or {})

    tiger_ids = set()
    for order in orders:
        try:
            # === 🧱 Liquidation Firewall & Cleanup ===
            if getattr(order, "liquidation", False) is True:
                trade_id = str(getattr(order, "order_id", ""))
                symbol = getattr(order, "symbol", "")
                filled_price = getattr(order, "filled_price", 0.0)
                quantity = getattr(order, "filled", 1)
                timestamp = getattr(order, "timestamp", "")
                
                print(f"🔥 Detected TigerTrade liquidation for {trade_id} – skipping open push.")

                # ✅ Delete from open_active_trades if exists
                open_active_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                open_active_trades = open_active_trades_ref.get() or {}
                if trade_id in open_active_trades:
                    print(f"🧹 Removing matching open trade {trade_id} due to liquidation.")
                    open_active_trades_ref.child(trade_id).delete()

                # ✅ Log to Google Sheets
                trade_data = {
                    "symbol": symbol,
                    "direction": order.get("action"),
                    "entry_price": filled_price,
                    "exit_price": filled_price,
                    "pnl_dollars": 0.0,
                    "reason_for_exit": "Liquidation",
                    "entry_time": timestamp,
                    "exit_time": timestamp,
                    "trail_triggered": "N/A",
                    "order_id": trade_id,
                    "exit_order_id": trade_id
                }
                try:
                    log_closed_trade_to_sheets(trade_data)
                    print(f"📄 Logged liquidation trade {trade_id} to Google Sheets.")
                except Exception as e:
                    print(f"❌ Failed to log liquidation {trade_id}: {e}")
                
                continue  # Skip pushing to /open_active_trades/

            oid = str(getattr(order, 'id', '')).strip()
            if not oid:
                print("⚠️ Skipping order with empty or missing ID")
                continue

            print(f"🔍 Processing order ID: {oid}")

            if is_zombie_trade(oid, db):
                print(f"⏭️ ⛔ Skipping zombie trade {oid} during API push")
                continue
            else:
                print(f"✅ Order ID {oid} not a zombie, proceeding")

            if is_archived_trade(oid, db):
                print(f"⏭️ ⛔ Skipping archived trade {oid} during API push")
                continue

            if is_ghost_trade(oid, db):
                print(f"⏭️ ⛔ Skipping ghost trade {oid} during API push (detected by helper)")
                continue
            else:
                print(f"✅ Order ID {oid} not a ghost, proceeding")

            tiger_ids.add(oid)

            # Extract order info
            exit_reason_raw = "UNKNOWN"
            status = str(getattr(order, "status", "")).split('.')[-1].upper()
            reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
            filled = getattr(order, "filled", 0)
            exit_reason_raw = get_exit_reason(status, reason, filled)

            # === Normalize TigerTrade timestamp (raw ms → ISO UTC) ===
            raw_ts = getattr(order, 'order_time', 0)
            try:
                ts_dt = datetime.utcfromtimestamp(raw_ts / 1000.0)
                exit_time_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")  # For Google Sheets
                exit_time_iso = ts_dt.isoformat() + "Z"              # For Firebase
            except Exception as e:
                print(f"⚠️ Failed to parse Tiger order_time: {raw_ts} → {e}")
                exit_time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                exit_time_iso = datetime.utcnow().isoformat() + "Z"
                exit_reason_raw = "UNKNOWN"

            print(f"ℹ️ Processed order ID: {oid}, status: {status}, reason: {reason}, filled: {filled}")

            # === DETECT GHOST TRADE ===
           # ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
           # is_ghost = filled == 0 and status in ghost_statuses
           # if is_ghost:
           #     print(f"👻 Ghost trade detected: {oid} (status={status}, filled={filled}) logged to ghost_trades_log")

            # Map friendly reason
            friendly_reason = REASON_MAP.get(exit_reason_raw, exit_reason_raw)

            # Parse symbol
            symbol = firebase_active_contract.get_active_contract()
            if not symbol:
                print(f"❌ No active contract symbol found in Firebase; skipping order ID {oid}")
                continue  # Skip processing this order

            # === BUILD PAYLOAD WITH PATCHED STATUS, TRADE_STATE, TRADE_TYPE ===
            payload = {
                "order_id": oid,
                "symbol": symbol,
                "action": str(getattr(order, 'action', '')).upper(),
                "quantity": getattr(order, 'quantity', 0),
                "filled": filled,
                "filled_price": getattr(order, 'avg_fill_price', 0.0),  # Exact filled price
                "status": status,             # e.g. FILLED, CANCELLED, EXPIRED
                "reason": friendly_reason,
                "liquidation": getattr(order, 'liquidation', False),
                "timestamp": exit_time_iso,
                "source": map_source(getattr(order, 'source', None)),
                "is_open": getattr(order, 'is_open', False),
               # "is_ghost": is_ghost,
                "exit_reason": friendly_reason,
                # Trade State and Trade Type logic for downstream usage
                "trade_state": "open" if status == "FILLED" else "closed",
                "trade_type": None  # Will be assigned below
            }

            # === Use trade_type from upstream (app.py) if available ===
            payload["trade_type"] = getattr(order, "trade_type", None) or None

            # ✅ Always push raw Tiger order into tiger_orders_log
            tiger_orders_ref.child(oid).set(payload)

            # Push to open_active_trades unconditionally
            trade_id = oid

            # === 🧱 Zombie Firewall: Skip if contracts_remaining missing or 0 === (COULD STOP ALL - SO THIS COULD CAUSE AND ISSUE)
               # 🟩 GREEN PATCH START: Contracts remaining check with 5-second grace period
            now = time.time()
            order_id = payload.get("order_id")

            contracts_remaining = payload.get("contracts_remaining")

            if contracts_remaining is None or contracts_remaining == 0:
                first_seen = grace_cache.get(order_id, now)
                grace_cache[order_id] = first_seen  # Set if new

                if now - first_seen < 5:  # 5 seconds grace period
                    print(f"⚠️ Grace period active for order {order_id}, allowing trade to be processed")
                    # Allow processing — do not skip
                else:
                    print(f"⏭️ Grace period expired for order {order_id}, archiving as zombie trade")
                    archived_ref = db.reference("/archive_trade_log")
                    archived_ref.child(order_id).set(payload)
                    continue  # Skip pushing to open trades
            else:
                # Clear cache for trades with contracts > 0
                if order_id in grace_cache:
                    del grace_cache[order_id]

                # Normal processing for trades with contracts_remaining > 0
            # 🟩 GREEN PATCH END

            # VALIDATE trade_id
            def is_valid_trade_id(tid):
                return isinstance(tid, str) and tid.isdigit()

            if not is_valid_trade_id(trade_id):
                print(f"❌ Aborting Firebase push due to invalid trade_id: {trade_id}")
                continue

            endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"

            # 🟩 GREEN PATCH START: Skip if ghost trade already archived this run
            if order_id in archived_trade_ids:
                print(f"⏭️ ⛔ Ghost trade {order_id} already archived this run; skipping duplicate archive")
                continue
            archived_trade_ids.add(order_id)
            # 🟩 GREEN PATCH END

            # ==========================
            # 🟩 GREEN PATCH START: Ghost Trade Filtering and Archiving
            # ==========================

            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}

            status = payload.get("status", "").upper()
            filled = payload.get("filled", 0)
            order_id = payload.get("order_id", "")

            if order_id in archived_trade_ids:
                print(f"⏭️ ⛔ Skipping archived trade {order_id} during API push")
                continue

           # if filled == 0 and status in ghost_statuses:
            #     print(f"🛑 Detected ghost trade {order_id} (status={status}, filled=0), archiving and skipping open trades push")
            #     print(f"    Full payload: {json.dumps(payload)}")

            #     # Archive ghost trade
            #     ghost_ref = db.reference("/ghost_trades_log")
            #     ghost_ref.child(order_id).set(payload)
            #     # Skip pushing this trade to open_active_trades
            #     continue

            # ==========================
            # 🟩 GREEN PATCH END
            # ==========================

            status = payload.get("status", "").upper()
            trade_state = payload.get("trade_state", "").lower()
            trade_id = payload.get("order_id", "")

            if status == "CLOSED" or trade_state == "closed":
                print(f"⚠️ Skipping closed trade {trade_id} for open_active_trades push")
                continue  # Skip pushing closed trades

            put = requests.put(endpoint, json=payload)
            if put.status_code == 200:
                print(f"✅ /open_active_trades/{symbol}/{trade_id} successfully updated")
            else:
                print(f"❌ Failed to update /open_active_trades/{symbol}/{trade_id}: {put.text}")

        except Exception as e:
            print(f"❌ Firebase push failed for {oid}: {e}")

    # ====================== GREEN PATCH END ======================

    # === Tally Summary ===
    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

    # === Ensure /open_active_trades/ path stays alive, even if no trades written ===
    try:
        open_active_trades_root = db.reference("/open_active_trades")
        snapshot = open_active_trades_root.get() or {}
        if not snapshot:
            print("🫀 Writing /open_active_trades/_heartbeat to keep path alive")
            open_active_trades_root.child("_heartbeat").set("alive")
    except Exception as e:
        print(f"❌ Failed to write /open_active_trades/_heartbeat: {e}")

#=====  END OF PART 2 =====
 
#=========================  PUSH_ORDERS_TO_FIREBASE - PART 3 (FINAL PART)  ================================

def log_closed_trade_to_google_sheet(trade):
    try:
        sheet = get_google_sheet()
        now_nz = datetime.now(timezone("Pacific/Auckland"))
        day_date = now_nz.strftime("%A %d %B %Y")
        exit_time_str = now_nz.strftime("%Y-%m-%d %H:%M:%S")

        exit_price = 0.0  # Optional placeholder
        pnl_dollars = 0.0
        exit_reason = trade.get("exit_reason", "manual_flattened")
        exit_method = "manual"
        exit_order_id = trade.get("exit_order_id", "MANUAL")

        sheet.append_row([
            day_date,
            trade.get("symbol", ""),
            "closed",
            trade.get("action", ""),
            trade.get("trade_type", ""),  # preserve trade_type here
            safe_float(trade.get("filled_price")),
            exit_price,
            pnl_dollars,
            exit_reason,
            trade.get("entry_timestamp", ""),
            exit_time_str,
            trade.get("trail_hit", False),
            trade.get("trade_id", ""),
            exit_order_id,
            exit_method
        ])
        print(f"✅ Logged closed trade to Google Sheets: {trade.get('trade_id', 'unknown')}")
    except Exception as e:
        print(f"❌ Failed to log trade to Google Sheets: {e}")

#=====  END OF PART 3 (END OF SCRIPT) =====