from fastapi import FastAPI, Request
import json
from datetime import datetime
import subprocess
import os
import requests
import pytz  # ✅ For NZ timezone
import random
import string
import gspread
from oauth2client.service_account import ServiceAccountCredentials


def generate_trade_id(symbol: str, side: str, qty: int) -> str:
    now = datetime.now(pytz.timezone("Pacific/Auckland"))
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    suffix = ''.join(random.choices(string.ascii_lowercase, k=2))
    return f"{symbol.lower()}_{timestamp}_{side.lower()}_{qty}_{suffix}"

app = FastAPI()

# === File paths ===
PRICE_FILE = "live_prices.json"
TRADE_LOG = "trade_log.json"
LOG_FILE = "app.log"
FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

# === Logging helper ===
def log_to_file(message: str):
    timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")


def get_google_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("gspread_credentials.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Trade Log").worksheet("Open Trades")
    return sheet

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    log_to_file(f"Webhook received: {data}")

    # === Handle Price Update ===
    if data.get("type") == "price_update":
        symbol = data.get("symbol")
        symbol = symbol.split("@")[0] if symbol else "UNKNOWN"
        try:
            price = float(data.get("price"))
        except (ValueError, TypeError):
            log_to_file("❌ Invalid price value received")
            return {"status": "error", "reason": "invalid price"}
        # Save live price locally
        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}
        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)
        # Push to Firebase
        nz_time = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
        payload = {"price": price, "updated_at": nz_time}
        log_to_file(f"📤 Pushing price to Firebase: {symbol} → {price}")
        try:
            requests.patch(f"{FIREBASE_URL}/live_prices/{symbol}.json", data=json.dumps(payload))
            log_to_file(f"✅ Price pushed: {price}")
        except Exception as e:
            log_to_file(f"❌ Firebase price push failed: {e}")
        return {"status": "price stored"}

    # === Handle Trade Signal ===
    elif data.get("action") in ("BUY", "SELL"):
        symbol = data["symbol"]
        action = data["action"]
        quantity = int(data.get("quantity", 1))

        # ✅ Added trade_id and timestamp (Fix #2)
        trade_id = generate_trade_id(symbol, action, quantity)
        entry_timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()

        # ✅ Submit trade to Tiger (Fix #1)
        log_to_file("[🧩] Entered trade execution block")

        try:
            result = subprocess.run([
                "python3", "execute_trade_live.py",
                symbol,
                action,
                str(quantity)
            ], capture_output=True, text=True, check=False)

            # === 🛠 PATCH: Log subprocess output to file (REMOVE AFTER DEBUGGING) ===
        with open("subprocess_exec.log", "a") as f:
            f.write(f"[STDOUT]\n{result.stdout}\n")
            f.write(f"[STDERR]\n{result.stderr}\n")
            f.write("-" * 40 + "\n")
        # === END PATCH BLOCK ===

            log_to_file(f"[🟡] Subprocess STDOUT: {result.stdout}")
            log_to_file(f"[🔴] Subprocess STDERR: {result.stderr}")

            if "✅ ORDER PLACED" in result.stdout:
                log_to_file("[✅] Trade confirmed by execute_trade_live.py — logging to Firebase and Sheets.")
            else:
                log_to_file("[❌] Subprocess did NOT confirm trade — skipping logging.")
                return {"status": "error", "message": "Trade execution failed (subprocess did not confirm)"}, 500

        except Exception as e:
            log_to_file(f"[🔥] Exception while running subprocess: {e}")
            return {"status": "error", "message": f"Subprocess exception: {e}"}, 500

        # === Fetch trailing settings from Firebase
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

        # === Load latest price
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

        # === Handle rejections
        if any(err in result.stderr.lower() for err in ["不支持", "not support", "error", "insufficient margin"]):
            log_to_file("⚠️ Trade rejected — logging ghost entry.")
            try:
                sheet = get_google_sheet()
                sheet.append_row([
                    trade_id, symbol, "REJECTED", action, 0,
                    trigger_points, offset_points,
                    False, "ghost_trade", entry_timestamp
                ])
                log_to_file("Ghost trade logged to Sheets.")
            except Exception as e:
                log_to_file(f"❌ Ghost sheet log failed: {e}")
            return {"status": "trade not filled"}

        # === Log to Google Sheets & Firebase
        for _ in range(quantity):
            try:
                sheet = get_google_sheet()
                sheet.append_row([
                    trade_id, symbol, price, action, 1,
                    trigger_points, offset_points,
                    True, "false", entry_timestamp
                ])
                log_to_file(f"Logged to Google Sheets: {trade_id}")
            except Exception as e:
                log_to_file(f"❌ Sheets log failed: {e}")

        try:
            endpoint = f"{FIREBASE_URL}/open_trades/{symbol}.json"
            resp = requests.get(endpoint)
            existing = resp.json() or []
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
                "entry_timestamp": entry_timestamp
            }
            existing.append(new_trade)
            put = requests.put(endpoint, json=existing)
            if put.status_code == 200:
                log_to_file("Firebase open_trades updated.")
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