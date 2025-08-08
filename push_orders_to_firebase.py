#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
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
import time

grace_cache = {}
_logged_trade_ids = set()

#================================
# 🟩 FIREBASE INITIALIZATION======
#================================

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

firebase_db = db

#================================
# 🟩 TIGER API SET UP ============
#================================
config = TigerOpenClientConfig()
client = TradeClient(config)

#################### ALL HELPERS FOR THIS SCRIPT ####################

# ====================================================
# 🟩 Helper: Google Sheets Setup (Global)
# ====================================================
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

def log_closed_trade_to_sheets(trade_data):
    print(f"Logging trade to sheets: {trade_data}")

# ===============================================================
# 🟩 Helper: Safe Float, Map Source, Get exit reason helpers ===
# ===============================================================
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

# =====================================================
# 🟩 Helper: Safe int cast for sorting entry_timestamps
# =====================================================
def safe_int(value):
    try:
        return int(value)
    except:
        return 0

# ====================================================
# 🟩 Helper: Load_trailing_tp_settings() From Firebase
# ====================================================
def load_trailing_tp_settings():
    try:
        ref = db.reference('/trailing_tp_settings')
        cfg = ref.get() or {}

        if cfg.get("enabled", False):
            return float(cfg.get("trigger_points", 14.0)), float(cfg.get("offset_points", 5.0))
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0

archived_trades_ref = db.reference("/archived_trades_log")
archived_trade_ids = set(archived_trades_ref.get() or {})
    

#===============================================
# 🟩 Helper: Check if Trade ID is a Known Zombie
#===============================================

def is_zombie_trade(trade_id, firebase_db):
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombies = zombie_ref.get() or {}
    return trade_id in zombies

#======================================================
# 🟩 Helper: Check if Trade ID is a Known Archived Trade
#======================================================

def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

# ====================================================
#🟩 Helper to Check if Trade ID is a Known Ghost Trade
# ====================================================
def is_ghostflag_trade(trade_id, firebase_db):
    ghost_ref = firebase_db.reference("/ghost_trades_log")
    ghosts = ghost_ref.get() or {}
    return trade_id in ghosts

#================================
#Archived_trade() helper function
#================================

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

#################### END OF ALL HELPERS FOR THIS SCRIPT ####################
    
# =======================================================
# ======MAIN FUNCTION ==PUSH ORDERS TO FIREBASE==========
# =======================================================
def push_orders_main():

    #=======Definitions ===========
    tiger_orders_ref = db.reference("/ghost_trades_log")  # rename from tiger_orders_log
    symbol = firebase_active_contract.get_active_contract()
    open_trades_ref = db.reference(f"open_active_trades/{symbol}")
    sheet = get_google_sheet()
    
    #=====Time function ===
    now = datetime.utcnow()
   
    #====Initialize counters here, BEFORE the order loop: =====
    filled_count = 0
    cancelled_count = 0
    lack_margin_count = 0
    unknown_count = 0

    # Reason map dictionary to more user friendly names
    REASON_MAP = {
        "trailing_tp_exit": "Trailing Take Profit",
        "manual_close": "Manual Close",
        "ema_flattening_exit": "EMA Flattening",
        "liquidation": "Liquidation",
        "LACK_OF_MARGIN": "Lack of Margin",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Lack of Margin",
    }

    # ================== Use Active Contract Symbol For Efficiency ====================
    # Before fetching orders from TigerTrade API, fetch the active contract from Firebase
    active_symbol = firebase_active_contract.get_active_contract()
    if not active_symbol:
        print("❌ No active contract symbol found in Firebase; aborting orders fetch")
        return 

    # Then pass active_symbol as a filter To Tiger Trade through client.get_orders()
    orders = client.get_orders(
        account="21807597867063647",
        seg_type=SegmentType.FUT,
        symbol=active_symbol,  # if you added this from patch
        limit=30
    )
    print(f"\n📦 Total orders returned for active contract {active_symbol}: {len(orders)}")

    #=========================================================================================
    # ====================== START THE FUNCTION: Push Orders Processing ======================
    #=========================================================================================

    # 🟩 Refresh archived trades cache inside main loop
    archived_trade_ids = set(archived_trades_ref.get() or {})

    tiger_ids = set()

    for order in orders:
        try:
            # =============== 🧱 Liquidation Firewall & Cleanup ===================================
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
                    "direction": getattr(order, "action", None),
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

            # ====================== 🧱 Liquidation Firewall & Cleanup END ===================================

            # ===================== Check if order ID is already processed and filter out ====================
            oid = str(getattr(order, 'id', '')).strip()
            # Skip if order ID is empty or None
            if not oid:
                print("⚠️ Skipping order with empty or missing ID")
                continue
            print(f"🔍 Processing order ID: {oid}")
            # Skip if Zombie Trade
            if is_zombie_trade(oid, db):
                print(f"⏭️ ⛔ Skipping zombie trade {oid} during API push")
                continue
            else:
                print(f"✅ Order ID {oid} not a zombie, proceeding")
            # Skip if already archived
            if is_archived_trade(oid, db):
                print(f"⏭️ ⛔ Skipping archived trade {oid} during API push")
                continue
            # Skip if ghost trade
            is_ghost_flag = is_ghostflag_trade(oid, db)
            if is_ghost_flag:
                print(f"⏭️ ⛔ Skipping ghost trade {oid} during API push (detected by helper)")
                continue
            else:
                print(f"✅ Order ID {oid} not a ghost, proceeding")

            # ===================================End of First filtering=======================================

           #=========================== Extract order information for further processing ====================
            tiger_ids.add(oid)

            exit_reason_raw = "UNKNOWN"
            status = str(getattr(order, "status", "")).split('.')[-1].upper()
            reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
            filled = getattr(order, "filled", 0)
            exit_reason_raw = get_exit_reason(status, reason, filled)

            # ======================= Normalize TigerTrade timestamp (raw ms → ISO UTC) ======================
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

            friendly_reason = REASON_MAP.get(exit_reason_raw, exit_reason_raw)

            symbol = firebase_active_contract.get_active_contract()
            if not symbol:
                print(f"❌ No active contract symbol found in Firebase; skipping order ID {oid}")
                continue  # Skip processing this order

            is_open = getattr(order, 'is_open', False)
            # ========================= BUILD PAYLOAD READY TO PUSH TO FIREBASE ====================================================
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
                "is_ghost": False,  # default, will update below if ghost detected
                "exit_reason": friendly_reason,
                # Trade State and Trade Type logic for downstream usage
                "trade_state": "open" if status == "FILLED" and is_open else "closed",
                "trade_type": getattr(order, "trade_type", None) or None
            }

            # ========================= DETECT GHOST TRADE ==================================================
            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
            is_ghost_flag = filled == 0 and status in ghost_statuses
            if is_ghost_flag:
                print(f"👻 Ghost trade detected: {oid} (status={status}, filled={filled}) logged to ghost_trades_log")

                # Update payload ghost flag
                payload["is_ghost"] = True

                # Write minimal info to ghost_trades_log to archive ghost trade
                ghost_ref = db.reference("/ghost_trades_log")
                ghost_ref.child(oid).set(payload)

                # Skip pushing this trade to open_active_trades
                continue

            # Re-check if already logged as ghost
            is_ghost_flag = is_ghostflag_trade(oid, db)

            # ===========🟩 Skip if trade already archived ghost or Zombie and cached in the new run coming up=====================
            if oid in archived_trade_ids:
                print(f"⏭️ ⛔ Archived trade {oid} already processed this run; skipping duplicate archive")
                continue

            if is_ghostflag_trade(oid, db):
                print(f"⏭️ ⛔ Skipping ghost trade {oid} during API push (detected by helper)")
                continue

            if is_zombie_trade(oid, db):
                print(f"⏭️ ⛔ Skipping zombie trade {oid} during API push (detected by helper)")
                continue

            print(f"Order ID {oid} not archived, ghost, or zombie; proceeding")

            archived_trade_ids.add(oid)

            # Define the validation function (can be outside the loop)
            def is_valid_trade_id(tid):
                return isinstance(tid, str) and tid.isdigit()

            # Extract and clean the raw order ID first
            oid = str(getattr(order, 'id', '')).strip()

            # Validate the raw order ID BEFORE doing anything else with it
            if not is_valid_trade_id(oid):
                print(f"❌ Skipping order due to invalid trade_id: {oid}")
                continue  # Skip this order and move to the next

            # Now assign trade_id safely, knowing it is valid
            trade_id = oid

            # Use trade_id safely from here on
            ref = db.reference(f'/open_active_trades/{symbol}/{trade_id}')

            #===========================End of Payload Preparation and sent to firebase ==================================================

            # ====================== Finalizing main function and Push to Firesbase an trade data for google sheets ======================

            trade_state = "open" if payload.get("is_open", True) else "closed"
            if not payload.get("is_open", True) or trade_state == "closed":
                # Prepare trade data for Google Sheets logging
                trade_data = {
                    "order_id": trade_id,
                    "entry_exit_time": exit_time_str,
                    "number_of_contracts": getattr(order, 'quantity', 1),
                    "trade_type": payload.get("trade_type", ""),
                    "fifo_match": "",  # Fill with actual FIFO match info if available
                    "entry_price": safe_float(payload.get("filled_price", 0.0)),
                    "close_price": safe_float(payload.get("filled_price", 0.0)),
                    "trail_trigger_value": payload.get("trail_trigger", 0),
                    "trail_offset": payload.get("trail_offset", 0),
                    "trailing_take_profit": 0,  # Add your calculation later
                    "trail_offset_amount": 0.0,  # Add your calculation later
                    "ema_flatten_type": "",  # Fill if applicable
                    "ema_flatten_triggered": "",  # Fill if applicable
                    "spread": 0.0,  # Add calculation or pass known value
                    "net_pnl": 0.0,  # Calculate and fill
                    "tiger_commissions": 0.0,  # Fill if known
                    "realised_pnl": 0.0,  # Calculate and fill
                    "fifo_match_order_id": "",  # Fill if known
                    "source": map_source(payload.get("source", None)),
                    "manual_notes": ""
                }
                log_closed_trade_to_sheets(trade_data)

                print(f"⚠️ Skipping closed trade {trade_id} for open_active_trades push")
                continue  # Skip pushing closed trades

            ref = firebase_db.reference(f"/open_active_trades/{symbol}/{trade_id}")
            try:
                ref.set(payload)
                print(f"✅ /open_active_trades/{symbol}/{trade_id} successfully updated")
            except Exception as e:
                print(f"❌ Failed to update /open_active_trades/{symbol}/{trade_id}: {e}")

        except Exception as e:
            print(f"❌ Error processing order {order}: {e}")
            continue

    # ============================== Tally Summary wrap up ================================
    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

    # ======= Ensure /open_active_trades/ path stays alive, even if no trades written =====
    try:
        open_active_trades_root = db.reference("/open_active_trades")
        snapshot = open_active_trades_root.get() or {}
        if not snapshot:
            print("🫀 Writing /open_active_trades/_heartbeat to keep path alive")
            open_active_trades_root.child("_heartbeat").set("alive")
    except Exception as e:
        print(f"❌ Failed to write /open_active_trades/_heartbeat: {e}")

#=============================================================================================================================================
#=============================================END OF MAIN PUSH_ORDERS_MAIN_FUNCTION===========================================================
#=============================================================================================================================================

#=================================================================================
#=========================  LOG TO GOOGLE SHEETS  ================================
#=================================================================================

def log_closed_trade_to_sheets(trade):
    trade_id = trade.get("order_id")
    if trade_id in _logged_trade_ids:
        print(f"⏭️ Trade {trade_id} already logged this session; skipping duplicate log.")
        return
    try:
        sheet = get_google_sheet()
        now_nz = datetime.now(timezone("Pacific/Auckland"))
        day_date = now_nz.strftime("%A %d %B %Y")
        
        sheet.append_row([
            day_date,
            trade.get("entry_exit_time", ""),
            trade.get("number_of_contracts", 1),
            trade.get("trade_type", ""),
            trade.get("fifo_match", "No"),
            safe_float(trade.get("entry_price", 0.0)),
            safe_float(trade.get("close_price", 0.0)),
            trade.get("trail_trigger_value", 0),
            trade.get("trail_offset", 0),
            trade.get("trailing_take_profit", 0),
            safe_float(trade.get("trail_offset_amount", 0.0)),
            trade.get("ema_flatten_type", ""),
            trade.get("ema_flatten_triggered", ""),
            safe_float(trade.get("spread", 0.0)),
            safe_float(trade.get("net_pnl", 0.0)),
            safe_float(trade.get("tiger_commissions", 0.0)),
            safe_float(trade.get("realised_pnl", 0.0)),
            trade_id,
            trade.get("fifo_match_order_id", ""),
            trade.get("source", ""),
            trade.get("manual_notes", "")
        ])
        print(f"✅ Logged closed trade {trade_id} to Google Sheets")
        _logged_trade_ids.add(trade_id)
    except Exception as e:
        print(f"❌ Failed to log trade to Google Sheets: {e}")

#===============================================================(END OF SCRIPT) ======================================================================