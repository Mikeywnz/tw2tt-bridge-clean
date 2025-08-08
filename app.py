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
position_tracker = {}
app = FastAPI()
recent_payloads = {}

DEDUP_WINDOW = 10  # seconds
PRICE_FILE = "live_prices.json"
TRADE_LOG = "trade_log.json"
LOG_FILE = "app.log"

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


#################### ALL HELPERS FOR THIS SCRIPT ####################

# ==============================================================
# 🟩 Helper: Google Sheets Setup - Get OPEN Trades Journal Sheet
# ==============================================================
def get_google_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("firebase_key.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Closed Trades Journal").worksheet("Open Trades Journal")
    return sheet

# ==============================================================
# 🟩 Helper: Log to file helper
# ==============================================================
def log_to_file(message: str):
    print(f"Logging: {message}")
    timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

# ==============================================================
# 🟩 Helper: Load Trailing TP Settings (Firebase Admin SDK)
# ==============================================================
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

# ==============================================================
# 🟩 Helper:IRONCLAD TRADE CLASSIFIER (Handles all cases cleanly)
# ==============================================================
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
 
# ==============================================================
# 🟩 Helper: Price updater
# ==============================================================
def perform_price_update(data):
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

#################### END OF ALL HELPERS FOR THIS SCRIPT ####################

# ====================================================================================================
# ===================================== MAIN FUNCTION ==APP WEBHOOK ===================================
# ====================================================================================================

@app.post("/webhook")
async def webhook(request: Request):
    current_time = time.time()
    #---------------------------------------------------
    # RECEIVING ORDER FORM TRADING VIEW WEBHOOK
    # ---------------------------------------------------
    try:
        data = await request.json()
        print("Logging data...")
        print(data)
        print("Finished data")
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    # -------------------------------------------------
    # PRICE UPDATE HANDLER
    # -------------------------------------------------
    # Handle price update immediately without dedupe
    action_type = data.get("type", "")
    if action_type == "price_update":
        return perform_price_update(data)
    
    # ---------------------------------------------------
    # TRADE ACTION HANDLER (BUY / SELL)
    # ---------------------------------------------------
    # Extract essential trade info safely
    request_symbol = data.get('symbol')
    action = data.get('action')
    quantity = data.get('quantity')

    if not request_symbol or not action or quantity is None:
        return {"status": "error", "message": "Missing required fields"}    
    
    # ---------------------------------------------------
    # DEDUPLICATION LOGIC
    # ---------------------------------------------------

    # ----------- Deduplication logic start --------------
    # Compute a hash of the payload for deduplication
    payload_str = json.dumps(data, sort_keys=True)
    payload_hash = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()

    # Cleanup old entries from dedupe cache
    for key in list(recent_payloads.keys()):
        if current_time - recent_payloads[key] > DEDUP_WINDOW:
            del recent_payloads[key]

    # Skip if duplicate within window
    if payload_hash in recent_payloads:
        print(f"⚠️ Duplicate webhook call detected; ignoring.")
        return {"status": "duplicate_skipped"}

    # Mark this payload as processed
    recent_payloads[payload_hash] = current_time

    print(f"[LOG] Webhook received: {data}")
    log_to_file(f"Webhook received: {data}")
    # ------------- Deduplication logic end --------------

    # ---------------------------------------------------
    # TRADE ORDER SENT TO EXECUTE TRADE LIVE
    # ---------------------------------------------------

    print(f"[DEBUG] Sending trade to execute_trade_live place_entry_trade()")
    result = place_entry_trade(request_symbol, action, quantity, firebase_db)
    print(f"[DEBUG] Received result from place_entry_trade: {result}")

    # === Guard clause: abort if trade_id invalid to avoid None bug ===
    def is_valid_trade_id(tid):
        return isinstance(tid, str) and tid.isdigit()

    if not is_valid_trade_id(result.get("order_id")):
        log_to_file(f"❌ Aborting Firebase push due to invalid trade_id: {result.get('order_id')}")
        return {"status": "error", "message": "Aborted push due to invalid trade_id"}, 555

    # Explicitly set status here for new trade
    status = "FILLED"  # You can adjust logic later if needed

    # Prevent trade_type being "closed" — remap to LONG_ENTRY or SHORT_ENTRY
    trade_type = result.get("trade_type", "").lower()
    if trade_type == "closed":
        if action.upper() == "BUY":
            trade_type = "LONG_ENTRY"
        else:
            trade_type = "SHORT_ENTRY"

    if not result.get("status") == "SUCCESS":
        log_to_file(f"❌ place_entry_trade failed or returned invalid result: {result}")
        try:
            ref = firebase_db.reference(f"/ghost_trades_log/{request_symbol}/{result.get('order_id')}")
            ref.set(data)  # Use original data or create new_trade dict if available
            log_to_file(f"✅ Firebase ghost_trades_log updated at key: {result.get('order_id')}")
        except Exception as e:
            log_to_file(f"❌ Firebase push error: {e}")
        return {"status": "error", "message": "Trade execution failed", "detail": result}

    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        log_to_file("❌ No active contract symbol found in Firebase; aborting trade action")
        return {"status": "error", "message": "No active contract symbol configured"}

    # Load trailing TP settings via Admin SDK, no Firebase URL string needed
    try:
        trigger_points, offset_points = load_trailing_tp_settings_admin(firebase_db)
    except Exception:
        trigger_points, offset_points = 14.0, 5.0

    # --- Order composed for Firebase ---
    new_trade = {
        "trade_id": result.get("order_id"),
        "symbol": symbol,
        "filled_price": result.get("filled_price", 0.0),
        "action": action,
        "trade_type": result.get("trade_type", ""),
        "status": "FILLED" if result.get("status", "UNKNOWN") == "SUCCESS" else "UNFILLED",
        "contracts_remaining": 1,
        "trail_trigger": trigger_points,
        "trail_offset": offset_points,
        "trail_hit": False,
        "trail_peak": result.get("filled_price", 0.0),
        "filled": True,
        "entry_timestamp": result.get("transaction_time", datetime.utcnow().isoformat() + "Z"),
        "trade_state": "open",
        "just_executed": True,
        "executed_timestamp": datetime.utcnow().isoformat() + "Z"
    }

    # --- Admin SDK: Push to Firebase ---
    try:
        ref = firebase_db.reference(f"/open_active_trades/{symbol}/{result.get('order_id')}")
        ref.set(new_trade)
        log_to_file(f"✅ Firebase open_active_trades updated at key: {result.get('order_id')}")
    except Exception as e:
        log_to_file(f"❌ Firebase push error: {e}")

    # ====================================================================================================
    # =============================✅ LOG TO GOOGLE SHEETS — OPEN TRADES JOURNAL ==========================
    # ====================================================================================================
    price = result.get("filled_price", 0.0)
    trade_id = result.get("order_id")
    entry_timestamp = result.get("transaction_time", datetime.utcnow().isoformat() + "Z")
    source = data.get("source", "webhook")
    symbol_for_log = request_symbol

    trade_type, updated_position = classify_trade(symbol_for_log, action, quantity, position_tracker, firebase_db)
    log_to_file(f"🟢 [LOG] Trade classified as: {trade_type}, updated net position: {updated_position}")

    for _ in range(quantity):
        try:
            day_date = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%A %d %B %Y")
            trail_trigger_price = round(price + trigger_points, 2)

            sheet.append_row([
                day_date,           # 1. day_date
                symbol_for_log,     # 2. symbol
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

    return {"status": "success", "message": "Trade processed"}

#=================================  (END OF SCRIPT) ======================================================