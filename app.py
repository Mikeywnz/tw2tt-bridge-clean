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
        entry_timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
        trade_id = generate_trade_id(symbol, action, quantity)
        log_to_file(f"Trade signal received: {data}")

        # === Fetch trailing settings from Firebase ===
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
            # === 🔄 Subprocess Trade Execution with Check ===
            try:
                result = subprocess.run([
                    "python3", "execute_trade_live.py",
                    symbol, action, str(quantity)
                ], capture_output=True, text=True)

                log_to_file(f"[🟡] Subprocess STDOUT: {result.stdout}")
                log_to_file(f"[🔴] Subprocess STDERR: {result.stderr}")

                # ✅ Check for clear success signal in stdout
                if "✅ ORDER PLACED" in result.stdout:
                    log_to_file("[✅] Trade confirmed by execute_trade_live.py — logging to Firebase and Sheets.")

                    # Continue logging the trade to Firebase, Sheets, and trade_log.json
                    # (Leave your existing Firebase logging code here)

                else:
                    log_to_file("[❌] Trade NOT confirmed — skipping Firebase log.")
                    # You can also log this ghost trade to Google Sheets if you want
                    # (Optional)

            except Exception as e:
                log_to_file(f"[💥] Subprocess failed: {e}")

        except Exception as e:
            log_to_file(f"Trade execution error: {e}")
            return {"status": "trade failed", "error": str(e)}

    return {"status": "ok"}