#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
import random
import string
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
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
_logged_order_ids = set()

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
    elif "liquidation" in lower:
        return "Tiger Liquidation"
    return "unknown"

def get_exit_reason(status, reason, filled, is_open=False):
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
archived_order_ids = set(archived_trades_ref.get() or {})
    
# ====================================================
# 🟩 Helper: to log payload
# ====================================================
def log_payload_as_closed_trade(payload):
    try:
        # Just log the raw payload dict (or with minimal sanitization if needed)
        print(f"Logging payload as closed trade: {payload}")
        # Your existing Google Sheets logging function can be called here if needed, e.g.:
        # log_closed_trade_to_sheets(payload)
    except Exception as e:
        print(f"❌ Failed to log payload as closed trade: {e}")

#===============================================
# 🟩 Helper: Check if Trade ID is a Known Zombie
#===============================================

def is_zombie_trade(order_id, firebase_db):
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombies = zombie_ref.get() or {}
    return order_id in zombies

#======================================================
# 🟩 Helper: Check if Trade ID is a Known Archived Trade
#======================================================

def is_archived_trade(order_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return order_id in archived_trades

# ====================================================
#🟩 Helper to Check if Trade ID is a Known Ghost Trade
# ====================================================
def is_ghostflag_trade(order_id, firebase_db):
    ghost_ref = firebase_db.reference("/ghost_trades_log")
    ghosts = ghost_ref.get() or {}
    return order_id in ghosts

#================================
#Archived_trade() helper function
#================================

def archive_trade(symbol, trade):
    order_id = trade.get("order_id")
    if not order_id:
        print(f"❌ Cannot archive trade without order_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{order_id}")  # <-- changed here
        # Preserve original trade_type; do NOT overwrite it with "closed"
        if "trade_type" not in trade or not trade["trade_type"]:
            trade["trade_type"] = "UNKNOWN"
        archive_ref.update(trade)
        print(f"✅ Archived trade {order_id}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {order_id}: {e}")
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
    open_trades_count = 0
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
    archived_order_ids = set(archived_trades_ref.get() or {})

    tiger_ids = set()

    for order in orders:
        try:
            # Always use the TigerTrade long ID from get_orders()
            order_id = str(getattr(order, 'id', '')).strip()

             # Single validation – ensures it's a numeric string
            if not (isinstance(order_id, str) and order_id.isdigit()):
                print(f"❌ Skipping order due to invalid order_id: '{order_id}'. Order raw data: {order}")
                continue

            if getattr(order, "liquidation", False) is True:
                symbol = getattr(order, "symbol", "")

                print(f"🔥 Detected TigerTrade liquidation for {order_id} – skipping open push.")

                # Delete from open_active_trades if exists
                open_active_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                open_active_trades = open_active_trades_ref.get() or {}
                if order_id in open_active_trades:
                    print(f"🧹 Removing matching open trade {order_id} due to liquidation.")
                    open_active_trades_ref.child(order_id).delete()

                # Log payload to Google Sheets via helper
                log_payload_as_closed_trade(order)

                # Archive payload to Firebase archive
                archived_ref = firebase_db.reference(f"/archived_trades_log/{order_id}")
                archived_ref.set(order)
                print(f"🗄️ Archived trade {order_id} to /archived_trades_log")

                # Skip pushing this liquidation trade to open_active_trades
                continue

                # ====================== 🧱 Liquidation Firewall & Cleanup END ===================================

            # ===================== Check if order ID is already processed and filter out ====================
            
            if not order_id:
                print("⚠️ Skipping order with empty or missing order_id")
                continue
            print(f"🔍 Processing order_id: {order_id}")

            if is_zombie_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping zombie trade {order_id} during API push")
                continue
            else:
                print(f"✅ Order ID {order_id} not a zombie, proceeding")

            if is_archived_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping archived trade {order_id} during API push")
                continue

            if is_ghostflag_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping ghost trade {order_id} during API push (detected by helper)")
                continue
            else:
                print(f"✅ Order ID {order_id} not a ghost, proceeding")

            # ===================================End of First filtering=======================================

           #=========================== Extract order information for further processing ====================
            
            tiger_ids.add(order_id)

            raw_status = getattr(order, "status", "")
            status = "FILLED" if raw_status == "SUCCESS" else str(raw_status).split('.')[-1].upper()
            raw_reason = getattr(order, "reason", "")
            filled = getattr(order, "filled", 0)
            is_open = getattr(order, "is_open", False)
            exit_reason_raw = get_exit_reason(status, raw_reason, filled, is_open)
            
            

            # ======================= Normalize TigerTrade timestamp (raw ms → ISO UTC) ======================
            raw_ts = getattr(order, 'order_time', 0)
            try:
                ts_dt = datetime.utcfromtimestamp(raw_ts / 1000.0)
                exit_time_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")  # For Google Sheets
            except Exception as e:
                print(f"⚠️ Failed to parse Tiger order_time: {raw_ts} → {e}")
                exit_time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                exit_reason_raw = "UNKNOWN"

            friendly_reason = REASON_MAP.get(exit_reason_raw, exit_reason_raw)

            symbol = firebase_active_contract.get_active_contract()
            if not symbol:
                print(f"❌ No active contract symbol found in Firebase; skipping order ID {order_id}")
                continue  # Skip processing this order

            order_data = order
            exit_timestamp = datetime.utcnow().isoformat() + "Z"
            entry_timestamp = getattr(order, "transaction_time", None)
            if entry_timestamp is None:
                entry_timestamp = datetime.utcnow().isoformat() + "Z" 

            trigger_points, offset_points = load_trailing_tp_settings()
            existing_trade = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").get() or {}
            filled_price_final = existing_trade.get("filled_price", 0.0)

            trade_type_final = existing_trade.get("trade_type", "")
            if getattr(order, "trade_type", None):
                trade_type_final = getattr(order, "trade_type")
            
            # ========================= BUILD PAYLOAD READY TO PUSH TO FIREBASE ====================================================
            print(f"[DEBUG] existing_trade data: {existing_trade}")
            print(f"[DEBUG] filled_price_final: {filled_price_final}")
            
            payload = {
                "order_id": order_id,
                "symbol": symbol,
                "exit_in_progress": False,
                "filled_price": filled_price_final,
                "action": str(getattr(order, 'action', '')).upper(),
                "trade_type": trade_type_final,
                "status": status,
                "contracts_remaining": getattr(order, "contracts_remaining", 1),
                "trail_trigger": existing_trade.get("trail_trigger", trigger_points),
                "trail_offset": existing_trade.get("trail_offset", offset_points),
                "trail_hit": False,
                "trail_peak": getattr(order, "filled_price", 0.0),
                "filled": bool(filled),
                "entry_timestamp": entry_timestamp,
                "just_executed": True,
                "exit_timestamp": exit_timestamp,
                "trade_state": "open" if status == "FILLED" and is_open else "closed",         
                "quantity": getattr(order, 'quantity', 0),
                "realized_pnl": 0.0,
                "net_pnl": 0.0,
                "tiger_commissions": 0.0,
                "reason": friendly_reason,
                "liquidation": getattr(order, 'liquidation', False),
                "source": map_source(getattr(order, 'source', None)),
                "is_open": getattr(order, 'is_open', False),
                "is_ghost": False,
                "exit_reason": friendly_reason,
            }
           

            ref = firebase_db.reference(f'/open_active_trades/{symbol}/{order_id}')
            try:
                existing_trade = ref.get() or {}
                merged_trade = {**existing_trade, **payload}
                ref.update(merged_trade)  # Use update() instead of set() to merge fields safely
                print(f"✅ /open_active_trades/{symbol}/{order_id} successfully merged and updated")
            except Exception as e:
                print(f"❌ Failed to update /open_active_trades/{symbol}/{order_id}: {e}")


            # ===== REPLACEMENT PATCH START FOR DETECT NO MANS LAND TRADES =====
            is_closed = not getattr(order, 'is_open', False) or str(getattr(order, 'status', '')).upper() in ['FILLED', 'CANCELLED', 'EXPIRED']

            order_id = payload.get("order_id", "")

            if is_closed:
                if order_id == "0" or not order_id:
                    print(f"[WARN] Skipping closed trade logging due to invalid order_id: {order_id}")
                    continue

                log_payload_as_closed_trade(payload)  # Log the existing payload data (trade data for sheets)
                print(f"✅ Logged closed trade {order_id} from payload")

                archived_ref = db.reference(f"/archived_trades_log/{order_id}")
                archived_ref.set(payload)
                print(f"🗄️ Archived trade {order_id} to /archived_trades_log")

                print(f"⚠️ Skipping closed trade {order_id} for open_active_trades push")
                continue
            # ===== NEW PATCH END =====
            # ========================= DETECT GHOST TRADE ==================================================
            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
            is_ghost_flag = filled == 0 and status in ghost_statuses
            if is_ghost_flag:
                print(f"👻 Ghost trade detected: {order_id} (status={status}, filled={filled}) logged to ghost_trades_log")

                # Update payload ghost flag
                payload["is_ghost"] = True

                # Write minimal info to ghost_trades_log to archive ghost trade
                ghost_ref = db.reference("/ghost_trades_log")
                ghost_ref.child(order_id).update(payload)

                # Skip pushing this trade to open_active_trades
                continue

            # Re-check if already logged as ghost
            is_ghost_flag = is_ghostflag_trade(order_id, db)

            # ===========🟩 Skip if trade already archived ghost or Zombie and cached in the new run coming up=====================
            if order_id in archived_order_ids:
                print(f"⏭️ ⛔ Archived trade {order_id} already processed this run; skipping duplicate archive")
                continue

            if is_ghostflag_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping ghost trade {order_id} during API push (detected by helper)")
                continue

            if is_zombie_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping zombie trade {order_id} during API push (detected by helper)")
                continue

            print(f"Order ID {order_id} not archived, ghost, or zombie; proceeding")

            if payload.get("is_open", False):
                open_trades_count += 1
            elif exit_reason_raw == "CANCELLED":
                cancelled_count += 1
            elif exit_reason_raw == "LACK_OF_MARGIN":
                lack_margin_count += 1
            else:
                unknown_count += 1

            archived_order_ids.add(order_id)

            # Use order_id safely from here on
            ref = db.reference(f'/open_active_trades/{symbol}/{order_id}')

            # Simple guard: skip closed but filled trades
            if not payload.get("is_open", False) and payload.get("status", "").upper() == "FILLED":
                print(f"⚠️ Skipping closed filled trade {payload.get('order_id')} before open check")
                continue


    #===========================End of Payload Preparation and sent to firebase ==================================================

    # =============================== Prepare trade data for google sheets =======================================================  

            if not payload.get("is_open", False):
                entry_price = safe_float(payload.get("entry_fill_price", 0.0))
                trailing_take_profit_points = payload.get("trail_trigger", 0)  # trigger distance in points
                trail_offset_points = payload.get("trail_offset", 0)          # offset buffer in points
                direction = 1 if payload.get("action", "").upper() == "BUY" else -1
                commissions = safe_float(payload.get("tiger_commissions", 7.02))
                trail_trigger_price = entry_price + (payload.get("trail_trigger", 0) * direction)

                # Calculate actual trailing take profit price level
                trailing_take_profit_price = entry_price + (trailing_take_profit_points * direction)

                exit_price = safe_float(payload.get("exit_fill_price", 0.0))

                # Calculate spread as actual exit price minus trigger price
                spread = exit_price - trail_trigger_price

                # trail_offset_amount is just the offset buffer points as float
                trail_offset_amount = float(trail_offset_points)

#                # Calculate net PnL
                net_pnl_value = safe_float(payload.get("realized_pnl", 0.0)) - commissions

                # Determine if this is an exit (flattening) trade
                is_exit_trade = payload.get("trade_type", "").startswith("FLATTENING") or payload.get("trade_type", "").startswith("EXIT")

                # Conditionally assign PNL fields for Google Sheets logging
                realized_pnl_value = "Match" if is_exit_trade else safe_float(payload.get("realized_pnl", 0.0))
                net_pnl_value = "Match" if is_exit_trade else safe_float(payload.get("net_pnl", 0.0))

  
                    
                trade_data = {
                    "order_id": order_id,
                    "entry_exit_time": exit_time_str,
                    "number_of_contracts": getattr(order, 'quantity', 1),
                    "trade_type": payload.get("trade_type", ""),
                    "fifo_match": payload.get("fifo_match", "NO"),
                    "entry_price": safe_float(payload.get("entry_fill_price", 0.0)),
                    "exit_price": safe_float(payload.get("exit_fill_price", 0.0)),
                    "trail_trigger_value": payload.get("trail_trigger", 0),
                    "trail_offset": payload.get("trail_offset", 0),
                    "trailing_take_profit": trailing_take_profit_price,
                    "trail_offset_amount": trail_offset_amount,
                    "ema_flatten_type": "N/A",
                    "ema_flatten_triggered": "N/A",
                    "spread": spread,
                    "realized_pnl": realized_pnl_value,
                    "tiger_commissions": commissions,
                    "net_pnl": net_pnl_value,
                    "fifo_match_order_id": payload.get("fifo_match_order_id", ""),
                    "source": map_source(payload.get("source", None)),
                    "manual_notes": ""
                }
                log_closed_trade_to_sheets(trade_data)

                print(f"⚠️ Skipping closed trade {order_id} for open_active_trades push")
                continue  # Skip pushing closed trades

        except Exception as e:
            print(f"❌ Error processing order {order}: {e}")
            continue

    # ============================== Tally Summary wrap up ================================
    print(f"✅ Open Trades: {open_trades_count}")
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
    order_id = trade.get("order_id")
    if order_id in _logged_order_ids:
        print(f"⏭️ Trade {order_id} already logged this session; skipping duplicate log.")
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
            safe_float(trade.get("exit_price", 0.0)),
            trade.get("trail_trigger_value", 0),
            trade.get("trail_offset", 0),
            trade.get("trailing_take_profit", 0),
            safe_float(trade.get("trail_offset_amount", 0.0)),
            trade.get("ema_flatten_type", ""),
            trade.get("ema_flatten_triggered", ""),
            safe_float(trade.get("spread", 0.0)),
            safe_float(trade.get("net_pnl", 0.0)),
            safe_float(trade.get("tiger_commissions", 0.0)),
            safe_float(trade.get("realized_pnl", 0.0)),
            order_id,
            trade.get("fifo_match_order_id", ""),
            trade.get("source", ""),
            trade.get("manual_notes", "")
        ])
        print(f"✅ Logged closed trade {order_id} to Google Sheets")
        _logged_order_ids.add(order_id)
    except Exception as e:
        print(f"❌ Failed to log trade to Google Sheets: {e}")

#===============================================================(END OF SCRIPT) ======================================================================