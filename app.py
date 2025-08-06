#=========================  APP.PY - PART 1  ================================
from fastapi import FastAPI, Request
import json
from datetime import datetime
import os
import requests
import pytz
import random
import string
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from execute_trade_live import place_entry_trade  # ✅ NEW: Import the function directly
import os
from firebase_admin import credentials, initialize_app, db
import firebase_active_contract
import firebase_admin
import time  # if not already imported
import hashlib

processed_exit_order_ids = set()

# Load Firebase secret key
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# Initialize Firebase Admin SDK
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

# Firebase database reference
firebase_db = db

# 🟢 ARCHIVED TRADE CHECK FUNCTION (updated path and logic)
#def is_archived_trade(trade_id: str, firebase_db) -> bool:
#    archived_ref = firebase_db.reference("/archived_trades_log")  # updated path
#    archived_trades = archived_ref.get() or {}
#    return trade_id in archived_trades

# 🟢 ZOMBIE TRADE CHECK FUNCTION
#def is_zombie_trade(trade_id: str, firebase_db) -> bool:
#    zombie_ref = firebase_db.reference("/zombie_trades_log")
#    zombie_trades = zombie_ref.get() or {}
#    return trade_id in zombie_trades

    # 🟢 GHOST TRADE CHECK FUNCTION
    #def is_ghost_trade(status: str, filled: int) -> bool:
        #ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
        #return filled == 0 and status in ghost_statuses
        #ghost_ref = firebase_db.reference(f"/ghost_trades_log/{trade_id}")
        #ghost_ref.set(trade_data)

    # Archive_trade helper
#def archive_trade(symbol, trade):
#    trade_id = trade.get("trade_id")
#    if not trade_id:
#        print(f"❌ Cannot archive trade without trade_id")
#        return False
#    try:
#        archive_ref = db.reference(f"/archived_trades_log/{trade_id}")
#        archive_ref.set(trade)
#        print(f"✅ Archived trade {trade_id}")
#        return True
#    except Exception as e:
#        print(f"❌ Failed to archive trade {trade_id}: {e}")
#        return False

# Global net position tracker dict
position_tracker = {}

app = FastAPI()

PRICE_FILE = "live_prices.json"
TRADE_LOG = "trade_log.json"
LOG_FILE = "app.log"


def log_to_file(message: str):
    timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

    # ✅ GOOGLE SHEETS: Get OPEN Trades Journal Sheet 
def get_google_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("firebase_key.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Closed Trades Journal").worksheet("Open Trades Journal")
    return sheet

# 🟩 FINAL – IRONCLAD TRADE CLASSIFIER (Handles all cases cleanly)
def classify_trade(symbol, action, qty, pos_tracker, fb_db):
    ttype = None  # Prevent NameError fallback

    # Fetch previous net position
    old_net = pos_tracker.get(symbol)
    if old_net is None:
        data = fb_db.reference(f"/live_total_positions/{symbol}").get() or {}
        old_net = int(data.get("position_count", 0))
        pos_tracker[symbol] = old_net

    # Determine direction
    buy = (action.upper() == "BUY")
    delta = qty if buy else -qty
    new_net = old_net + delta

    # 🧠 IRONCLAD LOGIC: 
    if old_net == 0:
        # When flat, any trade is an entry
        trade_type = "LONG_ENTRY" if buy else "SHORT_ENTRY"
        new_net = qty if buy else -qty

    elif old_net > 0:
        # Currently long
        trade_type = "LONG_ENTRY" if buy else "FLATTENING_SELL"

    elif old_net < 0:
        # Currently short
        trade_type = "FLATTENING_BUY" if buy else "SHORT_ENTRY"

    # Clamp new_net to 0 if it crosses over
    if (old_net > 0 and new_net < 0) or (old_net < 0 and new_net > 0):
        new_net = 0

    pos_tracker[symbol] = new_net
    return trade_type, new_net
 
# ========================= APP.PY - PART 2 ================================  
recent_payloads = {}
DEDUP_WINDOW = 10  # seconds

@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    current_time = time.time()

    # Fix: parse JSON once here only (remove later duplicate parsing)
    try:
        data = json.loads(raw_body)
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    # -----------------------------------
    # SPECIAL FLAGS: Liquidation & Manual
    # -----------------------------------
    if data.get("liquidation", False):
        source = "Liquidation"
    elif data.get("manual", False):
        source = "Manual"
    else:
        source = "OpGo"

    # -------------------------------
    # CLEANUP & DEDUPLICATION SECTION
    # -------------------------------
    for key in list(recent_payloads.keys()):
        if current_time - recent_payloads[key] > DEDUP_WINDOW:
            del recent_payloads[key]

    if data.get("type") != "price_update":
        if payload_hash in recent_payloads:
            print(f"⚠️ Duplicate webhook call detected; ignoring.")
            return {"status": "duplicate_skipped"}

    recent_payloads[payload_hash] = current_time
    log_to_file(f"Webhook received: {data}")

    # -----------------------
    # PRICE UPDATE HANDLER
    # -----------------------
    if data.get("type") == "price_update":
        symbol = data.get("symbol")
        symbol = symbol.split("@")[0] if symbol else "UNKNOWN"
        try:
            raw_price = data.get("price", "")
            if str(raw_price).upper() in ["MARKET", "MKT"]:
                try:
                    with open(PRICE_FILE, "r") as f:
                        prices = json.load(f)
                    price = float(prices.get(data.get("symbol", ""), 0.0))
                except Exception as e:
                    log_to_file(f"Price file fallback error: {e}")
                    price = 0.0
            else:
                price = float(raw_price)
        except (ValueError, TypeError):
            log_to_file("❌ Invalid price value received")
            return {"status": "error", "reason": "invalid price"}

        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}

        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)

        utc_time = datetime.utcnow().isoformat() + "Z"
        payload = {"price": price, "updated_at": utc_time}
        log_to_file(f"📤 Pushing price to Firebase: {symbol} → {price}")
        try:
            ref = firebase_db.reference(f"/live_prices/{symbol}")
            ref.update(payload)
            log_to_file(f"✅ Price pushed: {price}")
        except Exception as e:
            log_to_file(f"❌ Firebase price push failed: {e}")
        return {"status": "price stored"}

    # -----------------------------------
    # TRADE ACTION HANDLER (BUY / SELL)
    # -----------------------------------
    elif data.get("action") in ("BUY", "SELL"):
        symbol = firebase_active_contract.get_active_contract()
        if not symbol:
            log_to_file("❌ No active contract symbol found in Firebase; aborting trade action")
            return {"status": "error", "message": "No active contract symbol configured"}

        action = data["action"]
        log_to_file("🟢 [LOG] Received action from webhook: " + action)
        quantity = int(data.get("quantity", 1))

        # Load trailing TP settings via Admin SDK, no Firebase URL string needed
        try:
            trigger_points, offset_points = load_trailing_tp_settings_admin(firebase_db)
        except Exception:
            trigger_points, offset_points = 14.0, 5.0

        entry_timestamp = datetime.utcnow().isoformat() + "Z"
        trade_type = None  # Assign if you classify trades here, else set to None
    
# ============================
# 🟩 Load Trailing TP Settings (Firebase Admin SDK)
# ============================
def load_trailing_tp_settings_admin(firebase_db):
    print("[DEBUG] Starting to load trailing TP settings from Firebase (Admin SDK)")
    try:
        ref = firebase_db.reference("/trailing_tp_settings")
        cfg = ref.get() or {}
        print(f"[DEBUG] Trailing TP config fetched: {cfg}")
        if cfg.get("enabled", False):
            trigger_points = float(cfg.get("trigger_points", 14.0))
            offset_points = float(cfg.get("offset_points", 5.0))
            print(f"[DEBUG] Trailing TP enabled with trigger_points={trigger_points}, offset_points={offset_points}")
        else:
            trigger_points = 14.0
            offset_points = 5.0
            print("[DEBUG] Trailing TP disabled; using default values")
    except Exception as e:
        print(f"[WARN] Exception loading trailing TP settings: {e}")
        trigger_points = 14.0
        offset_points = 5.0

    print(f"[DEBUG] Returning trailing TP settings: trigger_points={trigger_points}, offset_points={offset_points}")
    return trigger_points, offset_points

# =========================================
# 🟩 NEW TRADE CREATION AND FIREBASE UPDATE
# =========================================

# ==========================================
# 🟩 Webhook Handler (Entry Trade Processing) - Admin SDK Firebase Writes
# ==========================================
def webhook_handler(data, firebase_db):
    print("[DEBUG] webhook_handler started")

    # --- Order comes in from TradingView ---
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found; aborting")
        return {"status": "error", "message": "No active contract symbol"}

    action = data.get("action")
    quantity = int(data.get("quantity", 1))
    print(f"[DEBUG] Received trade action: {action}, quantity: {quantity}")

    # --- Order classified ---
    trade_type, updated_position = classify_trade(symbol, action, quantity, position_tracker, firebase_db)
    print(f"[DEBUG] Trade classified as: {trade_type}, updated position: {updated_position}")

    # --- Load trailing take profit settings ---
    trigger_points, offset_points = load_trailing_tp_settings()
    entry_timestamp = datetime.utcnow().isoformat() + "Z"
    print(f"[DEBUG] Entry timestamp for trade: {entry_timestamp}")

    # --- Order sent to execute_trade_live ---aa
    print(f"[DEBUG] Sending trade to execute_trade_live place_entry_trade()")
    result = place_entry_trade(symbol, action, quantity, firebase_db)
    print(f"[DEBUG] Received result from place_entry_trade: {result}")

    # --- Order received from execute_trade_live and validated ---
    if isinstance(result, dict) and result.get("status") == "SUCCESS":

        def is_valid_trade_id(tid):
            return isinstance(tid, str) and tid.isdigit()

        raw = result.get("order_id")
        if isinstance(raw, int):
            trade_id = str(raw)
        elif isinstance(raw, str):
            trade_id = raw
        else:
            trade_id = None

        if not trade_id or not is_valid_trade_id(trade_id):
            log_to_file(f"❌ Invalid trade_id detected: {trade_id}")
            return {"status": "error", "message": "Invalid trade_id from execute_trade_live"}, 555

        status = result.get("trade_status", "UNKNOWN")
        filled_price = result.get("filled_price") or 0.0

        # --- Order composed for Firebase ---
        new_trade = {
            "trade_id": trade_id,
            "symbol": symbol,
            "filled_price": filled_price,
            "action": action,
            "trade_type": trade_type,
            "status": status,
            "contracts_remaining": 1,
            "trail_trigger": trigger_points,
            "trail_offset": offset_points,
            "trail_hit": False,
            "trail_peak": filled_price,
            "filled": True,
            "entry_timestamp": entry_timestamp,
            "trade_state": "open",
            "just_executed": True,
            "executed_timestamp": datetime.utcnow().isoformat() + "Z"
        }

        # --- Admin SDK: Push to Firebase ---
        try:
            ref = firebase_db.reference(f"/open_active_trades/{symbol}/{trade_id}")
            ref.set(new_trade)
            log_to_file(f"✅ Firebase open_active_trades updated at key: {trade_id}")
        except Exception as e:
            log_to_file(f"❌ Firebase push error: {e}")

    return {"status": "success", "message": "Trade processed"}

    # ========================= APP.PY - PART 3 (FINAL PART) ================================

    # ✅ LOG TO GOOGLE SHEETS — OPEN TRADES JOURNAL
    trade_type, updated_position = classify_trade(symbol, action, quantity, position_tracker, firebase_db)
    log_to_file(f"🟢 [LOG] Trade classified as: {trade_type}, updated net position: {updated_position}")

    for _ in range(quantity):
        try:
            day_date = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%A %d %B %Y")
            trail_trigger_price = round(price + trigger_points, 2)

            sheet.append_row([
                day_date,           # 1. day_date
                symbol,             # 2. symbol
                action,             # 3. action
                trade_type,         # 4. Short or Long (new column)
                price,              # 5. filled_price
                trigger_points,     # 6. trail_trigger (pts)
                offset_points,      # 7. trail_offset (pts)
                trail_trigger_price,# 8. trigger_price
                trade_id,           # 9. tiger_order_id
                entry_timestamp,    # 10. entry_time (UTC)
                source              # 11. Where did the trade come from?
            ])

            log_to_file(f"Logged to Open Trades Sheet: {trade_id}")
        except Exception as e:
            log_to_file(f"❌ Open sheet log failed: {e}")

    # === Guard clause: abort if trade_id invalid to avoid None bug ===
    def is_valid_trade_id(tid):
        return isinstance(tid, str) and tid.isdigit()

    if not is_valid_trade_id(trade_id):
        log_to_file(f"❌ Aborting Firebase push due to invalid trade_id: {trade_id}")
        return {"status": "error", "message": "Aborted push due to invalid trade_id"}, 555

    # Explicitly set status here for new trade
    status = "FILLED"  # You can adjust logic later if needed

    # Prevent trade_type being "closed" — remap to LONG_ENTRY or SHORT_ENTRY
    if trade_type.lower() == "closed":
        if action.upper() == "BUY":
            trade_type = "LONG_ENTRY"
        else:
            trade_type = "SHORT_ENTRY"

    new_trade = {
        "trade_id": trade_id,
        "symbol": symbol,
        "filled_price": filled_price,
        "action": action,
        "trade_type": trade_type,
        "status": status,
        "contracts_remaining": 1,
        "trail_trigger": trigger_points,
        "trail_offset": offset_points,
        "trail_hit": False,
        "trail_peak": filled_price,
        "filled": True,
        "entry_timestamp": entry_timestamp,
        "trade_state": "open",
        "just_executed": True,
        "executed_timestamp": datetime.utcnow().isoformat() + "Z"
    }

    try:
        log_to_file("🟢 [LOG] Pushing trade to Firebase with payload: " + json.dumps(new_trade))
        put = requests.put(endpoint, json=new_trade)
        if put.status_code == 200:
            log_to_file(f"✅ Firebase open_active_trades updated at key: {trade_id}")
        else:
            log_to_file(f"❌ Firebase update failed: {put.text}")
    except Exception as e:
        log_to_file(f"❌ Firebase push error: {e}")

    try:
        entry = {
            "timestamp": entry_timestamp,
            "trade_id": trade_id,
            "symbol": symbol,
            "action": action,
            "price": price,
            "quantity": quantity
        }
        logs = []
        if os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "r") as f:
                logs = json.load(f)
        logs.append(entry)
        with open(TRADE_LOG, "w") as f:
            json.dump(logs, f, indent=2)
        log_to_file("Logged to trade_log.json.")
    except Exception as e:
        log_to_file(f"❌ trade_log.json failed: {e}")

    return {"status": "ok"}

    #=====  END OF PART 3 (END OF SCRIPT) =====