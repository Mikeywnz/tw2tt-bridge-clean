from fastapi import FastAPI, Request
import json
from datetime import datetime
import subprocess
import csv
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
OPEN_TRADES_FILE = "open_trades.csv"
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
    sheet = client.open("Trade Log").worksheet("Open Trades")  # 🔁 Update name if needed
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

        # ✅ Push price to Firebase with NZT timestamp
        nz_time = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
        payload = {
            "price": price,
            "updated_at": nz_time
        }

        log_to_file(f"📤 Pushing to Firebase: {symbol} → price={price}")

        try:
            requests.patch(f"{FIREBASE_URL}/live_prices/{symbol}.json", data=json.dumps(payload))
            log_to_file(f"✅ Pushed to Firebase: price={price}")
        except Exception as e:
            log_to_file(f"❌ Failed to push to Firebase: {e}")

        return {"status": "price stored"}

    # === Handle Trade Signal ===
    elif data.get("action") in ("BUY", "SELL"):
        symbol = data["symbol"]
        action = data["action"]
        quantity = int(data.get("quantity", 1))
        entry_timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
        trade_id = generate_trade_id(symbol, action, quantity)

        log_to_file(f"Trade signal received: {data}")
        try:
            result = subprocess.run([
                "python3", "execute_trade_live.py",
                symbol, action, str(quantity)
            ], capture_output=True, text=True)

            log_to_file(f"Executed TigerTrade: stdout={result.stdout.strip()} stderr={result.stderr.strip()}")

            # Load latest price
            try:
                with open(PRICE_FILE, "r") as f:
                    prices = json.load(f)
                    price = float(prices.get(symbol, 0.0))
            except Exception as e:
                log_to_file(f"Could not load price: {e}")
                price = 0.0

            # Check for rejection
            if any(error in result.stderr.lower() for error in [
                "不支持", "not support", "error", "insufficient margin"
            ]):
                log_to_file("⚠️ TigerTrade order rejected — skipping CSV log.")
                return {"status": "trade not filled"}

            # ✅ Log each contract to CSV
            for _ in range(quantity):
                with open(OPEN_TRADES_FILE, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade_id,
                        symbol,
                        price,
                        action,
                        1,
                        0.4,
                        0.2,
                        "",           # Placeholder for removed ema50
                        True,
                        False,         # ✅ trail_hit = False
                        entry_timestamp
                    ])

                log_to_file(f"📩 Trade logged to open_trades.csv: {symbol} {action} @ {price}")

                try:
                    sheet = get_google_sheet()
                    sheet.append_row([
                        trade_id,
                        symbol,
                        price,
                        action,
                        1,
                        0.4,
                        0.2,
                        "",           # Placeholder for removed ema50
                        True,
                        "false",       # ✅ trail_hit = false in Sheets
                        entry_timestamp
                    ])
                    log_to_file(f"📋 Trade also logged to Google Sheets: {trade_id}")
                except Exception as e:
                    log_to_file(f"❌ Google Sheets logging failed: {e}")

            # ✅ Also log to Firebase
            try:
                firebase_key = f"/open_trades/{symbol}.json"
                firebase_endpoint = FIREBASE_URL + firebase_key

                response = requests.get(firebase_endpoint)
                existing = response.json() or []

                new_trade = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "entry_price": price,
                    "action": action,
                    "contracts_remaining": 1,
                    "trail_trigger": 0.004,
                    "trail_offset": 0.002,
                    "tp_trail_price": None,
                    "trail_hit": False,
                    "trail_peak": price,
                    "filled": True,
                    "entry_timestamp": entry_timestamp
                }

                existing.append(new_trade)
                log_to_file("📦 Pushing trade to Firebase")
                put_response = requests.put(firebase_endpoint, json=existing)

                if put_response.status_code == 200:
                    log_to_file(f"✅ Trade also pushed to Firebase for {symbol}")
                else:
                    log_to_file(f"❌ Failed to push trade to Firebase: {put_response.text}")
            except Exception as e:
                log_to_file(f"❌ Firebase push error: {e}")

            # Log to JSON trade log
            try:
                log_entry = {
                    "timestamp": entry_timestamp,
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "action": action,
                    "price": price,
                    "quantity": quantity
                }

                if os.path.exists(TRADE_LOG):
                    with open(TRADE_LOG, "r") as f:
                        logs = json.load(f)
                else:
                    logs = []

                logs.append(log_entry)
                with open(TRADE_LOG, "w") as f:
                    json.dump(logs, f, indent=2)

                log_to_file("🧾 Trade also logged to trade_log.json")

            except Exception as e:
                log_to_file(f"❌ Failed to log to trade_log.json: {e}")

        except Exception as e:
            log_to_file(f"TigerTrade execution failed: {e}")
            return {"status": "trade failed", "error": str(e)}

    return {"status": "ok"}