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
import time  # if not already imported

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

# ==========================
# 🟩 HELPER: Update Trade on Exit Fill (Exit Order Confirmation Handler)
# ==========================
def update_trade_on_exit_fill(firebase_db, symbol, exit_order_id, exit_action, filled_qty):
    global processed_exit_order_ids
    if exit_order_id in processed_exit_order_ids:
        print(f"[DEBUG] Exit order {exit_order_id} already processed, skipping update.")
        return True
    processed_exit_order_ids.add(exit_order_id)

    print(f"[DEBUG] update_trade_on_exit_fill() called for exit_order_id={exit_order_id}")

    open_active_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    open_trades = open_active_trades_ref.get() or {}

    opposite_action = "BUY" if exit_action == "SELL" else "SELL"
    matching_trade_id = None

    for trade_id, trade in open_trades.items():
        print(f"[DEBUG] Checking trade {trade_id} with action {trade.get('action')} exited={trade.get('exited')}")
        if trade.get("action") == opposite_action and not trade.get("exited"):
            matching_trade_id = trade_id
            print(f"[DEBUG] Found matching trade {trade_id} for exit_action {exit_action}")
            break

    if not matching_trade_id:
        print(f"[WARN] No matching open trade found for exit action {exit_action} on symbol {symbol}")
        return False

    trade_ref = open_active_trades_ref.child(matching_trade_id)
    print(f"[DEBUG] Marking trade {matching_trade_id} as exited with filled_qty={filled_qty}")

    try:
        trade_ref.update({
            "exited": True,
            "contracts_remaining": 0,
            "exit_order_id": exit_order_id,
            "exit_action": exit_action,
            "exit_filled_qty": filled_qty,
            "trade_state": "closed"
        })
        # Confirm update by reading back
        updated_trade = trade_ref.get()
        print(f"[DEBUG] After update, trade state: exited={updated_trade.get('exited')}, trade_state={updated_trade.get('trade_state')}")
        print(f"[INFO] Updated trade {matching_trade_id} as exited in Firebase")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to update trade {matching_trade_id}: {e}")
        return False

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
    #def is_ghost_trade(status: str, filled: int) -> bool:
        #ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
        #return filled == 0 and status in ghost_statuses
        #ghost_ref = firebase_db.reference(f"/ghost_trades_log/{symbol}/{trade_id}")
        #ghost_ref.set(trade_data)

    # Archive_trade helper
def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{trade_id}")
        archive_ref.set(trade)
        print(f"✅ Archived trade {trade_id}")
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

    # 🟢 classify_trade: Determine trade type and update net position

    #OLD VERSION INCASE NEW VERSION BREAKS EVRYTHIGN (HOWEVER THIS VERSION NOT CURRENTLY WORKING)
#def classify_trade(symbol, action, qty, pos_tracker, fb_db):
 #   old_net = pos_tracker.get(symbol)
  #  if old_net is None:
   #     data = fb_db.reference(f"/live_total_positions/{symbol}").get() or {}
    #    old_net = int(data.get("position_count", 0))
     #   pos_tracker[symbol] = old_net

  #  buy = (action.upper() == "BUY")

 #   if old_net == 0:
 #      ttype = "LONG_ENTRY" if buy else "SHORT_ENTRY"
 #       new_net = qty if buy else -qty
 #   else:
 #       if (old_net > 0 and buy) or (old_net < 0 and not buy):
 #           ttype = "LONG_ENTRY" if buy else "SHORT_ENTRY"
 #           new_net = old_net + (qty if buy else -qty)
 #       else:
 #           ttype = "FLATTENING_BUY" if buy else "FLATTENING_SELL"
 #           new_net = old_net + (qty if buy else -qty)
 #           if (buy and new_net > 0) or (not buy and new_net < 0):
 #               new_net = 0

 #   print(f"[DEBUG] {symbol}: action={action}, qty={qty}, old_net={old_net}, new_net={new_net}, trade_type={ttype}") #DUBUG

 #   pos_tracker[symbol] = new_net
 #   return ttype, new_net

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

        # Price update block removed fallback price loading for market order
        try:
            price = float(data.get("price", 0.0))
        except Exception as e:
            log_to_file("❌ Invalid price value received")
            return {"status": "error", "reason": "invalid price"}

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

        # Load trailing TP settings before trade execution
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

        # No fallback price logic — pure market order flow
        entry_timestamp = datetime.utcnow().isoformat() + "Z"
        log_to_file("[🧩] Entered trade execution block")

        try:
            # === CHECK exit_in_progress FLAG BEFORE PLACING EXIT ORDER ===
            open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
            open_trades = open_trades_ref.get() or {}

            # Find matching trade with same action & not exited (simplified logic)
            matching_trade_id = None
            for tid, trade in open_trades.items():
                if trade.get("action") == action and not trade.get("exited") and not trade.get("exit_in_progress"):
                    matching_trade_id = tid
                    break

            if matching_trade_id:
                trade_data = open_trades[matching_trade_id]
                if trade_data.get("exit_in_progress"):
                    log_to_file(f"⚠️ Exit already in progress for trade {matching_trade_id}, skipping exit order placement")
                    return {"status": "skipped", "reason": "exit_in_progress"}

            result = place_trade(symbol, action, quantity)

            # After successful exit order placement, set exit_in_progress fl
            if isinstance(result, dict) and result.get("status") == "SUCCESS":
                if matching_trade_id:
                    # Retry updating exit_in_progress flag until confirmed or timeout (5 seconds max)
                    max_retries = 5
                    retry_count = 0
                    while retry_count < max_retries:
                        try:
                            open_trades_ref.child(matching_trade_id).update({"exit_in_progress": True})
                            # Verify update by reading back
                            updated_trade = open_trades_ref.child(matching_trade_id).get()
                            if updated_trade and updated_trade.get("exit_in_progress") == True:
                                log_to_file(f"🟢 Confirmed exit_in_progress=True for trade {matching_trade_id}")
                                break
                        except Exception as e:
                            log_to_file(f"⚠️ Retry {retry_count+1}: Failed to set exit_in_progress: {e}")
                        retry_count += 1
                        time.sleep(1)
                    else:
                        log_to_file(f"❌ Failed to confirm exit_in_progress flag for trade {matching_trade_id} after {max_retries} retries")

            # =========================
            # 🟩 PATCH 2: Update trade on exit fills in webhook POST route
            # =========================

            if action.upper() in ["BUY", "SELL"]:
                if "exit_order_id" in result and result["exit_order_id"]:
                    exit_order_id = result["exit_order_id"]
                    exit_action = action
                    filled_qty = result.get("filled_quantity", 0)
                    update_trade_on_exit_fill(firebase_db, symbol, exit_order_id, exit_action, filled_qty)
            
            log_to_file(f"🟢 place_trade result: {result}")

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
                filled = result.get("filled_quantity", 0)

                if is_archived_trade(trade_id, firebase_db):
                    log_to_file(f"⏭️ Ignoring archived trade {trade_id} in webhook")
                    return {"status": "skipped", "reason": "archived trade"}

                if is_zombie_trade(trade_id, firebase_db):
                    log_to_file(f"⏭️ Ignoring zombie trade {trade_id} in webhook")
                    return {"status": "skipped", "reason": "zombie trade"}

                log_to_file(f"[✅] Valid Tiger Order ID received: {trade_id}")
                data["trade_id"] = trade_id

                filled_price = result.get("filled_price") or 0.0
                
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

                endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"
                log_to_file("🟢 [LOG] Pushing trade to Firebase with payload: " + json.dumps(new_trade))
                put = requests.put(endpoint, json=new_trade)
                if put.status_code == 200:
                    log_to_file(f"✅ Firebase open_active_trades updated at key: {trade_id}")
                else:
                    log_to_file(f"❌ Firebase update failed: {put.text}")

            else:
                log_to_file(f"[❌] Trade result: {result}")
                return {"status": "error", "message": f"Trade result: {result}"}, 555

        except Exception as e:
            log_to_file(f"❌ Exception in place_trade: {e}")
            return {"status": "error", "message": f"Exception in place_trade: {e}"}, 555
        # ======= END OF PART 2 =======

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