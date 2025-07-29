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
from execute_trade_live import place_trade  # ✅ NEW: Import the function directly
import os
from firebase_admin import credentials, initialize_app, db
import firebase_active_contract
import firebase_admin

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
def is_archived_trade(trade_id: str, firebase_db) -> bool:
    archived_ref = firebase_db.reference("/archived_trades_log")  # updated path
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

# 🟢 ZOMBIE TRADE CHECK FUNCTION
def is_zombie_trade(trade_id: str, firebase_db) -> bool:
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombie_trades = zombie_ref.get() or {}
    return trade_id in zombie_trades

    # 🟢 GHOST TRADE CHECK FUNCTION
def is_ghost_trade(status: str, filled: int) -> bool:
    ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
    return filled == 0 and status in ghost_statuses
    ghost_ref = firebase_db.reference(f"/ghost_trades_log/{symbol}/{trade_id}")
    ghost_ref.set(trade_data)

    # Archive_trade helper
def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{symbol}/{trade_id}")
        archive_ref.set(trade)
        print(f"✅ Archived trade {trade_id} under {symbol}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {trade_id}: {e}")
        return False

# Global net position tracker dict
position_tracker = {}

app = FastAPI()

PRICE_FILE = "live_prices.json"
TRADE_LOG = "trade_log.json"
LOG_FILE = "app.log"
FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

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

    # 🟢 classify_trade: Determine trade type and update net position
def classify_trade(symbol, action, qty, pos_tracker, fb_db):
    old_net = pos_tracker.get(symbol)
    if old_net is None:
        data = fb_db.reference(f"/live_total_positions/{symbol}").get() or {}
        old_net = int(data.get("position_count", 0))
        pos_tracker[symbol] = old_net

    buy = (action.upper() == "BUY")

    if old_net == 0:
        ttype = "LONG_ENTRY" if buy else "SHORT_ENTRY"
        new_net = qty if buy else -qty
    else:
        if (old_net > 0 and buy) or (old_net < 0 and not buy):
            ttype = "LONG_ENTRY" if buy else "SHORT_ENTRY"
            new_net = old_net + (qty if buy else -qty)
        else:
            ttype = "FLATTENING_BUY" if buy else "FLATTENING_SELL"
            new_net = old_net + (qty if buy else -qty)
            if (buy and new_net > 0) or (not buy and new_net < 0):
                new_net = 0

    print(f"[DEBUG] {symbol}: action={action}, qty={qty}, old_net={old_net}, new_net={new_net}, trade_type={ttype}") #DUBUG

    pos_tracker[symbol] = new_net
    return ttype, new_net

        #=====  END OF PART 1 =====

 # ========================= APP.PY - PART 2 ================================  

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    log_to_file(f"Webhook received: {data}")
    sheet = get_google_sheet()

    if data.get("liquidation", False):
        source = "Liquidation"
    elif data.get("manual", False):
        source = "Manual"
    else:
        source = "OpGo"

    if data.get("type") == "price_update":
        symbol = firebase_active_contract.get_active_contract()
        if not symbol:
            log_to_file("❌ No active contract symbol found in Firebase; aborting price update")
            return {"status": "error", "message": "No active contract symbol configured"}

        try:
            # === PATCH: Allow "MARKET" or "MKT" fallback price from file ===
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
            requests.patch(f"{FIREBASE_URL}/live_prices/{symbol}.json", data=json.dumps(payload))
            log_to_file(f"✅ Price pushed: {price}")
        except Exception as e:
            log_to_file(f"❌ Firebase price push failed: {e}")
        return {"status": "price stored"}

    elif data.get("action") in ("BUY", "SELL"):
        symbol = firebase_active_contract.get_active_contract()
        if not symbol:
            log_to_file("❌ No active contract symbol found in Firebase; aborting trade action")
            return {"status": "error", "message": "No active contract symbol configured"}
        action = data["action"]
        log_to_file("🟢 [LOG] Received action from webhook: " + action)
        quantity = int(data.get("quantity", 1))

        trade_type, updated_position = classify_trade(symbol, action, quantity, position_tracker, firebase_db)
        log_to_file(f"🟢 [LOG] Trade classified as: {trade_type}, updated net position: {updated_position}")

     # ========================= GREEN PATCH START: MARKET ORDER PRICE FALLBACK & TRADE EXECUTION =========================

        # === MARKET ORDER PRICE FALLBACK ===
        price = None
        raw_price = data.get("price", "")  # Allow optional price in alert

        if raw_price == "" or str(raw_price).upper() in ["MARKET", "MKT"]:
            # Load live price from file as fallback
            try:
                with open(PRICE_FILE, "r") as f:
                    prices = json.load(f)
                price = float(prices.get(symbol, 0.0))
            except Exception as e:
                log_to_file(f"Price file fallback error in trade alert: {e}")
                price = 0.0
        else:
            try:
                price = float(raw_price)
            except Exception as e:
                log_to_file(f"Invalid explicit price in trade alert: {e}")
                price = 0.0

        if price <= 0:
            log_to_file(f"❌ Invalid entry price {price} for market order fallback; aborting trade for {symbol}")
            return {"status": "error", "message": "invalid entry price for market order fallback"}

        # ✅ FETCH Tiger Order ID + Timestamp from Execution
        entry_timestamp = datetime.utcnow().isoformat() + "Z"
        log_to_file("[🧩] Entered trade execution block")

        # ======= START PATCH: place_trade try/except block with status and filled extraction =======
        try:
            result = place_trade(symbol, action, quantity)
            log_to_file(f"🟢 place_trade result: {result}")

            if isinstance(result, dict) and result.get("status") == "SUCCESS":
                # === Simplified trade ID extraction and validation ===
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

                # Extract status and filled quantity for ghost trade detection
                status = result.get("trade_status", "UNKNOWN")
                filled = result.get("filled_quantity", 0)

                # 🟢 FILTER ARCHIVED AND ZOMBIE TRADES BEFORE PROCESSING
                if is_archived_trade(trade_id, firebase_db):
                    log_to_file(f"⏭️ Ignoring archived trade {trade_id} in webhook")
                    return {"status": "skipped", "reason": "archived trade"}

                if is_zombie_trade(trade_id, firebase_db):
                    log_to_file(f"⏭️ Ignoring zombie trade {trade_id} in webhook")
                    return {"status": "skipped", "reason": "zombie trade"}

                # GHOST TRADE DETECTION (now safe to use status and filled)
                if is_ghost_trade(status, filled, trade_id, firebase_db):
                    log_to_file(f"⏭️ Ignoring ghost trade {trade_id} in webhook")
                    return {"status": "skipped", "reason": "ghost trade"}

                log_to_file(f"[✅] Valid Tiger Order ID received: {trade_id}")
                data["trade_id"] = trade_id

            else:
                log_to_file(f"[❌] Trade result: {result}")
                return {"status": "error", "message": f"Trade result: {result}"}, 555

        except Exception as e:
            log_to_file(f"❌ Exception in place_trade: {e}")
            return {"status": "error", "message": f"Exception in place_trade: {e}"}, 555
        # ======= END PATCH =======

               #=====  END OF PART 2 =====

# ========================= APP.PY - PART 3 (FINAL PART) ================================

        entry_timestamp = datetime.utcnow().isoformat() + "Z"

        try:
            fb_url = f"{FIREBASE_URL}/trailing_tp_settings.json"
            res = requests.get(fb_url)
            cfg = res.json() if res.ok else {}
            if cfg.get("enabled", False):
                trigger_points = float(cfg.get("trigger_points", 14.0))
                offset_points = float(cfg.get("offset_points", 5.0))
            else:
                trigger_points = 14.0
                offset_points = 5.0
        except Exception as e:
            log_to_file(f"[WARN] Failed to fetch trailing settings, using defaults: {e}")
            trigger_points = 14.0
            offset_points = 5.0

        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
                price = float(prices.get(symbol, 0.0))
        except Exception as e:
            log_to_file(f"Price load error: {e}")
            price = 0.0

        if price <= 0:
            log_to_file("❌ Invalid entry price (0.0) – aborting log.")
            return {"status": "invalid entry price"}

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
                    price,              # 5. entry_price
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
            "entry_price": price,
            "action": action,
            "trade_type": trade_type,
            "status": status,
            "contracts_remaining": 1,
            "trail_trigger": trigger_points,
            "trail_offset": offset_points,
            "trail_hit": False,
            "trail_peak": price,
            "filled": True,
            "entry_timestamp": entry_timestamp,
            "trade_state": "open"  # Newly opened trade
        }

        endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"
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