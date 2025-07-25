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

FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

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
    
# === Setup Tiger API ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Firebase Init ===
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

logged_ghost_ids_ref = db.reference("/logged_ghost_ids")
logged_ghost_order_ids = set(logged_ghost_ids_ref.get() or [])

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
    sheet = gs_client.open("Closed Trades Journal").worksheet("Open Trades Journal")
    return sheet

# === STEP 3A: Helper to Check if Trade ID is a Known Zombie ===
def is_zombie_trade(trade_id, firebase_db):
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombies = zombie_ref.get() or {}
    return trade_id in zombies

    # ===== Archived_trade() helper function =====
def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/tiger_orders_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

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

    tiger_orders_ref = db.reference("/tiger_orders_log")
    open_trades_ref = db.reference("open_active_trades")
    pos_tracker = {}

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

    # === Time Window: last 48 hours ===
    now = datetime.utcnow()
    start_time = now - timedelta(hours=48)
    end_time = now

    orders = client.get_orders(
        account="21807597867063647",
        seg_type=SegmentType.FUT,
        start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
        limit=150
    )

    print(f"\n📦 Total orders returned: {len(orders)}")

    tiger_ids = set()
    for order in orders:
        try:
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

            tiger_ids.add(oid)

            # Extract order info
            status = str(getattr(order, "status", "")).split('.')[-1].upper()
            reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
            filled = getattr(order, "filled", 0)
            exit_reason_raw = ""
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

        except Exception as e:
            print(f"❌ Exception processing order: {e}")

            # Detect ghost
            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
            filled = getattr(order, "filled", 0)
            is_ghost = filled == 0 and status in ghost_statuses

            # Map friendly reason
            friendly_reason = REASON_MAP.get(exit_reason_raw, exit_reason_raw)

            # Parse symbol
            raw_contract = str(getattr(order, 'contract', ''))
            symbol = raw_contract.split('/')[0] if '/' in raw_contract else raw_contract

            # Construct payload
            payload = {
                "order_id": oid,
                "symbol": symbol,
                "action": str(getattr(order, 'action', '')).upper(),
                "quantity": getattr(order, 'quantity', 0),
                "filled": filled,
                "avg_fill_price": getattr(order, 'avg_fill_price', 0.0),
                "status": status,
                "reason": friendly_reason,
                "liquidation": getattr(order, 'liquidation', False),
                "timestamp": exit_time_iso,
                "source": map_source(getattr(order, 'source', None)),
                "is_open": getattr(order, 'is_open', False),
                "is_ghost": is_ghost,
                "exit_reason": friendly_reason
            }

            # ✅ Always push raw Tiger order into tiger_orders_log
            tiger_orders_ref.child(oid).set(payload)
            # === STEP 2 FIFI EXIT MATCHING LOGIC: If this is a closing trade, match it to an open trade ===
            action = payload.get("action")
            symbol = payload.get("symbol")
            order_time = payload.get("order_time")
            opposite_action = "SELL" if action == "BUY" else "BUY"

            # Get current open trades for this symbol
            open_trades = open_trades_ref.child(symbol).get() or {}

            # ✅ Skip any trade not marked as open (prevents reprocessing closed trades)
            matched_trade_id = None
            for tid, trade in sorted(open_trades.items(), key=lambda item: safe_int(item[1].get("entry_timestamp", 0))):
                if trade.get("action") == opposite_action and trade.get("trade_state", "open") == "open":
                    matched_trade_id = tid
                    break

            # If we found a match, mark it as closed and remove it
            if matched_trade_id:
                print(f"🟡 Matched closing trade: {action} {symbol} → closes {opposite_action} {matched_trade_id}")

                # ⏱️ Convert Tiger order_time to ISO
                try:
                    ts_int = int(order_time)
                    if ts_int > 1e12:
                        ts_int //= 1000
                    exit_time_iso = datetime.utcfromtimestamp(ts_int).isoformat() + 'Z'
                except Exception:
                    exit_time_iso = None

                open_trades_ref.child(symbol).child(matched_trade_id).update({
                    "trade_state": "closed",
                    "exit_reason": friendly_reason,
                    "exit_method": "fifo",
                    "exit_order_id": oid,
                    "exit_time_iso": exit_time_iso
                })

                # Remove from /open_trades/
                open_trades_ref.child(symbol).child(matched_trade_id).delete()
                print(f"✅ Removed from /open_active_trades/: {symbol}/{matched_trade_id}")
         
                print(f"✅ Pushed to Firebase Tiger Orders Log: {oid}")

                # ✅ PATCH: Prevent re-adding closed FIFO trades to open_active_trades
                trade_id = oid  # Use the Tiger order ID extracted earlier, not payload.get("trade_id")
                symbol = payload.get("symbol")

                firebase_trade = open_trades.get(symbol, {}).get(trade_id)

                # If this trade already exists in Firebase and has an exit reason, skip it
                if firebase_trade and firebase_trade.get("exit_reason") in ["FIFO Close", "manual_flattened", "Liquidation", "manual_close"]:
                    print(f"⛔️ Skipping re-add of closed trade {trade_id} with exit_reason: {firebase_trade.get('exit_reason')}")
                    continue
                price = payload["avg_fill_price"]
                action = payload["action"]
                entry_timestamp = getattr(order, "order_time", None)
                trigger_points, offset_points = load_trailing_tp_settings()

                endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"

                ttype, net_pos = classify_trade(symbol, action, 1, pos_tracker, db)

                new_trade = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "entry_price": price,
                    "action": action,
                    "trade_type": ttype,
                    "contracts_remaining": 1,
                    "trail_trigger": trigger_points,
                    "trail_offset": offset_points,
                    "trail_hit": False,
                    "trail_peak": price,
                    "filled": True,
                    "entry_timestamp": entry_timestamp,
                    "exit_reason": "",
                    "trade_state": "open"
                }

                def is_valid_trade_id(tid):
                    return isinstance(tid, str) and tid.isdigit()

                if not is_valid_trade_id(trade_id):
                    print(f"❌ Aborting Firebase push due to invalid trade_id: {trade_id}")
                    continue  # Skip this trade, do not push invalid IDs

                put = requests.put(endpoint, json=new_trade)

                if put.status_code == 200:
                    print(f"✅ /open_active_trades/{symbol}/{trade_id} successfully updated")
                else:
                    print(f"❌ Failed to update /open_active_trades/{symbol}/{trade_id}: {put.text}")

        except Exception as e:
            print(f"❌ Firebase push failed for {oid}: {e}")

    # === Tally Summary ===
    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

    # === Ensure /open_active_trades/ path stays alive, even if no trades written ===
    try:
        open_trades_root = db.reference("/open_active_trades")
        snapshot = open_trades_root.get() or {}
        if not snapshot:
            print("🫀 Writing /open_active_trades/_heartbeat to keep path alive")
            open_trades_root.child("_heartbeat").set("alive")
    except Exception as e:
        print(f"❌ Failed to write /open_active_trades/_heartbeat: {e}")

#=====  END OF PART 2 =====
 
#=========================  PUSH_ORDERS_TO_FIREBASE - PART 3 (FINAL PART)  ================================

def log_closed_trade_to_google_sheet(trade):
    try:
        now_nz = datetime.now(timezone("Pacific/Auckland"))
        day_date = now_nz.strftime("%A %d %B %Y")
        exit_time_str = now_nz.strftime("%Y-%m-%d %H:%M:%S")

        exit_price = 0.0  # Optional placeholder for now
        pnl_dollars = 0.0
        exit_reason = "manual_flattened"
        exit_method = "manual"
        exit_order_id = "MANUAL"

        sheet.append_row([
            day_date,                                # 0
            trade.get("symbol", ""),                 # 1
            "closed",                               # 2
            trade.get("action", ""),                 # 3
            trade.get("trade_type", ""),             # 4  NEW trade_type column
            safe_float(trade.get("entry_price")),   # 5 (shifted)
            exit_price,                             # 6
            pnl_dollars,                            # 7
            exit_reason,                            # 8
            trade.get("entry_timestamp", ""),       # 9
            exit_time_str,                          # 10
            trade.get("trail_hit", False),          # 11
            trade.get("trade_id", ""),              # 12
            exit_order_id,                          # 13
            exit_method                             # 14
        ])
        print(f"✅ Logged closed trade to Google Sheets: {trade.get('trade_id', 'unknown')}")
    except Exception as e:
        print(f"❌ Failed to log trade to Google Sheets: {e}")

#=====  END OF PART 3 (END OF SCRIPT) =====