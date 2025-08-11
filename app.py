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
from google.oauth2.service_account import Credentials
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
    payload = data.copy()  # Make a copy of the incoming data as payload

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
    print(f"[DEBUG] Filled price from result: {result.get('filled_price')}")

      # Build the payload dict with real API values from 'result' and known data
    payload = {
        "trade_id": result.get("order_id"),
        "symbol": request_symbol,
        "filled_price": result.get("filled_price", 0.0),
        "action": action.upper(),
        "trade_type": result.get("trade_type", ""),
        "status": result.get("status", "UNKNOWN"),
        "contracts_remaining": quantity,
        "trail_trigger": trigger_points,    # loaded trigger points from your config
        "trail_offset": offset_points,      # loaded offset points from your config
        "trail_hit": False,
        "trail_peak": result.get("filled_price", 0.0),
        "filled": True,
        "entry_timestamp": result.get("transaction_time", datetime.utcnow().isoformat() + "Z"),
        "trade_state": "open",
        "just_executed": True,
        "executed_timestamp": datetime.utcnow().isoformat() + "Z",
        "quantity": quantity,
        "reason": "",  # Optional: Add if available
        "liquidation": False,
        "source": map_source("OpGo"),
        "is_open": True,
        "is_ghost": False,
        "exit_reason": "",
    }
    

    # === Guard clause: abort if trade_id invalid to avoid None bug ===
    def is_valid_trade_id(tid):
        return isinstance(tid, str) and tid.isdigit()

    if not is_valid_trade_id(result.get("order_id")):
        log_to_file(f"❌ Aborting Firebase push due to invalid trade_id: {result.get('order_id')}")
        print(f"❌ Aborting Firebase push due to invalid trade_id: {result.get('order_id')}")
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
        print(f"❌ place_entry_trade failed or returned invalid result: {result}")
        try:
            ref = firebase_db.reference(f"/ghost_trades_log/{request_symbol}/{result.get('order_id')}")
            ref.set(data)  # Use original data or create new_trade dict if available
            log_to_file(f"✅ Firebase ghost_trades_log updated at key: {result.get('order_id')}")
            print(f"✅ Firebase ghost_trades_log updated at key: {result.get('order_id')}")
        except Exception as e:
            log_to_file(f"❌ Firebase push error: {e}")
            print(f"❌ Firebase push error: {e}")
        return {"status": "error", "message": "Trade execution failed", "detail": result}

        # Get active symbol from Firebase or your config
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found; aborting new trade creation")
        return

    # Load trailing TP settings from admin SDK or use defaults
    try:
        trigger_points, offset_points = load_trailing_tp_settings_admin(firebase_db)
    except Exception:
        trigger_points, offset_points = 14.0, 5.0

    # Assume 'result' and 'payload' come from your previous API call or processing
    # For example, 'result' from Tiger API trade placement response
    # 'payload' is your existing dictionary with trade info (can be empty or partial)

    # Action string from your trade context
    action = payload.get("action", "BUY").upper()

    # Build new_trade dict merging existing payload to preserve info
    new_trade = {
        "trade_id": result.get("order_id"),
        "symbol": symbol,
        "filled_price": result.get("filled_price", 0.0),
        "action": action,
        "trade_type": result.get("trade_type", payload.get("trade_type", "")),
        "status": "FILLED" if result.get("status", "UNKNOWN") == "SUCCESS" else "UNFILLED",
        "contracts_remaining": payload.get("contracts_remaining", 1),
        "trail_trigger": trigger_points,
        "trail_offset": offset_points,
        "trail_hit": payload.get("trail_hit", False),
        "trail_peak": payload.get("trail_peak", result.get("filled_price", 0.0)),
        "filled": payload.get("filled", True),
        "entry_timestamp": result.get("transaction_time", datetime.utcnow().isoformat() + "Z"),
        "timestamp": exit_time_iso,
        "trade_state": payload.get("trade_state", "open"),
        "just_executed": True,
        "realized_pnl": payload.get("realized_pnl", 0.0),
        "net_pnl": payload.get("net_pnl", 0.0),
        "tiger_commissions": payload.get("tiger_commissions", 0.0),
        "executed_timestamp": datetime.utcnow().isoformat() + "Z",
        "quantity": payload.get("quantity", 1),
        "reason": payload.get("reason", ""),
        "liquidation": payload.get("liquidation", False),
        "source": map_source(payload.get("source", None)),
        "is_open": payload.get("is_open", True),
        "is_ghost": payload.get("is_ghost", False),
        "exit_reason": payload.get("exit_reason", ""),
    }
     # Merge new_trade with existing trade data (if any)
    print(f"[DEBUG] New trade payload to push to Firebase: {new_trade}")
    try:
        ref = firebase_db.reference(f"/open_active_trades/{symbol}/{new_trade['trade_id']}")
        existing_trade = ref.get() or {}
        merged_trade = {**existing_trade, **new_trade}  # Merge to preserve prior data
        ref.update(merged_trade)
        print(f"✅ Firebase open_active_trades updated at key: {new_trade['trade_id']}")
    except Exception as e:
        print(f"❌ Firebase push error: {e}")

    # --- Admin SDK: Push to Firebase ---
   
    # ====================================================================================================
    # =============================✅ LOG TO GOOGLE SHEETS — NOW CLOSED TRADES JOURNAL ==========================
    # ====================================================================================================
    price = safe_float(result.get("filled_price", 0.0))
    trade_id = result.get("order_id")
    entry_timestamp = result.get("transaction_time", datetime.utcnow().isoformat() + "Z")
    source = data.get("source", "webhook")
    symbol_for_log = request_symbol

    trade_type, updated_position = classify_trade(symbol_for_log, action, quantity, position_tracker, firebase_db)
    print(f"🟢 [LOG] Trade classified as: {trade_type}, updated net position: {updated_position}")

    # Calculate trailing TP and offset amounts
    trigger_points = trigger_points if 'trigger_points' in locals() else 14.0  # fallback default
    offset_points = offset_points if 'offset_points' in locals() else 5.0      # fallback default
    direction = 1 if action.upper() == "BUY" else -1
    trailing_take_profit_price = price + (trigger_points * direction)
    trail_offset_amount = float(offset_points)

    trade_data = {
        "order_id": trade_id,
        "entry_exit_time": entry_timestamp or "N/A",
        "number_of_contracts": quantity,
        "trade_type": trade_type,
        "fifo_match": "No",                    # Placeholder for FIFO match status
        "fifo_match_order_id": "N/A",         # Placeholder for FIFO match order id
        "entry_price": price,
        "exit_price": "N/A",
        "trail_trigger_value": trigger_points,
        "trail_offset": offset_points,
        "trailing_take_profit": trailing_take_profit_price,
        "trail_offset_amount": trail_offset_amount,
        "ema_flatten_type": "N/A",
        "ema_flatten_triggered": "N/A",
        "spread": "N/A",
        "net_pnl": "N/A",
        "tiger_commissions": "N/A",
        "realized_pnl": "N/A",
        "source": map_source(source),
        "manual_notes": ""
    }

    try:
        day_date = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%A %d %B %Y")

        sheet.append_row([
            day_date,                                # 1. Day Date
            entry_timestamp,                         # 2. Entry/Exit Time
            trade_data.get("number_of_contracts", 1),  # 3. Number of Contracts
            trade_data.get("trade_type", ""),          # 4. Trade Type (Short/Long)
            trade_data.get("fifo_match", "No"),         # 5. FIFO Match
            safe_float(trade_data.get("entry_price", 0.0)),  # 6. Entry Price
            trade_data.get("exit_price", "N/A"),            # 7. Exit Price
            trade_data.get("trail_trigger_value", 0),       # 8. Trail Trigger Value
            trade_data.get("trail_offset", 0),               # 9. Trail Offset
            trade_data.get("trailing_take_profit", 0),       # 10. Trailing Take Profit Hit
            safe_float(trade_data.get("trail_offset_amount", 0.0)),  # 11. Trail Offset $ Amount
            trade_data.get("ema_flatten_type", "N/A"),          # 12. EMA Flatten Type
            trade_data.get("ema_flatten_triggered", "N/A"),     # 13. EMA Flatten Triggered
            safe_float(trade_data.get("spread", 0.0)),          # 14. Spread
            safe_float(trade_data.get("net_pnl", 0.0)),         # 15. Net PnL
            safe_float(trade_data.get("tiger_commissions", 0.0)),  # 16. Tiger Commissions
            safe_float(trade_data.get("realized_pnl", 0.0)),     # 17. Realized PnL
            trade_id,                                           # 18. Order ID
            trade_data.get("fifo_match_order_id", "N/A"),      # 19. FIFO Match Order ID
            trade_data.get("source", ""),                        # 20. Source
            trade_data.get("manual_notes", "")                   # 21. Manually Filled Notes
        ])

        # Call your helper to log to Google Sheets (if you want to log full data as well)
        log_closed_trade_to_sheets(trade_data)

        print(f"✅ Logged to Close Trades Sheet: {trade_id}")
    except Exception as e:
        print(f"❌ Close sheet log failed: {e}")

    return {"status": "success", "message": "Trade processed"}

    # =============================== END OF SCRIPT =======================================================  

