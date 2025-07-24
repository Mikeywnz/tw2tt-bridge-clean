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
    creds = ServiceAccountCredentials.from_json_keyfile_name("gspread_credentials.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Closed Trades Journal").worksheet("Open Trades Journal")
    return sheet

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    log_to_file(f"Webhook received: {data}")

    if data.get("type") == "price_update":
        symbol = data.get("symbol")
        symbol = symbol.split("@")[0] if symbol else "UNKNOWN"
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
        symbol = data["symbol"]
        action = data["action"]
        quantity = int(data.get("quantity", 1))

        #=====  END OF PART 1 =====

 #=========================  APP.PY - PART 2 (FINAL PART) ================================       

        # ✅ FETCH Tiger Order ID + Timestamp from Execution
        entry_timestamp = datetime.utcnow().isoformat() + "Z"
        log_to_file("[🧩] Entered trade execution block")

        try:
            result = place_trade(symbol, action, quantity)
            if isinstance(result, dict) and result.get("status") == "SUCCESS":
                trade_id = result.get("order_id")
                log_to_file(f"[✅] Tiger Order ID received: {trade_id}")
            else:
                log_to_file(f"[❌] Trade result: {result}")
                return {"status": "error", "message": f"Trade result: {result}"}, 555
        except Exception as e:
            log_to_file(f"[🔥] Trade execution error: {e}")
            return {"status": "error", "message": "Trade execution failed"}, 555

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
        for _ in range(quantity):
            try:
                day_date = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%A %d %B %Y")
                trail_trigger_price = round(price + trigger_points, 2)

                sheet.append_row([
                    day_date,           # 1. day_date
                    symbol,             # 2. symbol
                    action,             # 3. action
                    price,              # 4. entry_price
                    trigger_points,     # 5. trail_trigger (pts)
                    offset_points,      # 6. trail_offset (pts)
                    trail_trigger_price,# 7. trigger_price
                    trade_id,           # 8. tiger_order_id
                    entry_timestamp     # 9. entry_time (UTC)
                ])

                log_to_file(f"Logged to Open Trades Sheet: {trade_id}")
            except Exception as e:
                log_to_file(f"❌ Open sheet log failed: {e}")

        # ✅ PUSH trade to Firebase under /open_trades/{symbol}/{order_id}
        try:
            new_trade = {
                "trade_id": trade_id,
                "symbol": symbol,
                "entry_price": price,
                "action": action,
                "contracts_remaining": 1,
                "trail_trigger": trigger_points,
                "trail_offset": offset_points,
                "trail_hit": False,
                "trail_peak": price,
                "filled": True,
                "entry_timestamp": entry_timestamp,  # UTC
                "status": "open"  
            }

            endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"
            put = requests.put(endpoint, json=new_trade)
            print(f"[APP.PY] Firebase push URL: {endpoint}")
            print(f"[APP.PY] Payload: {json.dumps(new_trade, indent=2)}")
            print(f"[APP.PY] Firebase push status: {put.status_code} → {put.text}")
            if put.status_code == 200:
                log_to_file(f"✅ Firebase open_active_trades updated at key: {trade_id}")
            else:
                log_to_file(f"❌ Firebase update failed: {put.text}")
        except Exception as e:
            log_to_file(f"❌ Firebase push error: {e}")

        try:
            entry = {
                "timestamp": entry_timestamp,  # UTC
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

    #=====  END OF PART 2 (END OF SCRIPT) =====