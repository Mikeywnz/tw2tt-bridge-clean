from fastapi import FastAPI, Request
import json
from datetime import datetime
import subprocess
import csv
import os

app = FastAPI()

# === File paths ===
PRICE_FILE = "live_prices.json"
EMA_FILE = "ema_values.json"
TRADE_LOG = "trade_log.json"
OPEN_TRADES_FILE = "open_trades.csv"
LOG_FILE = "app.log"

# === Logging helper ===
def log_to_file(message: str):
    timestamp = datetime.utcnow().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        print(f"❌ Failed to parse JSON: {e}")
        log_to_file(f"Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    print(f"📥 Incoming Webhook: {data}")
    log_to_file(f"Webhook received: {data}")

    # === Handle Price Update ===
    if data.get("type") == "price_update":
        symbol = data["symbol"]
        price = float(data["price"])

        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}

        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)

        print(f"💾 Stored live price: {symbol} = {price}")
        log_to_file(f"Stored live price: {symbol} = {price}")
        return {"status": "price stored"}

    # === Handle EMA Update ===
    elif data.get("type") == "ema_update":
        symbol = data["symbol"]
        ema9 = float(data["ema9"])
        ema20 = float(data["ema20"])

        try:
            with open(EMA_FILE, "r") as f:
                ema_data = json.load(f)
        except FileNotFoundError:
            ema_data = {}

        ema_data[symbol] = {
            "ema9": ema9,
            "ema20": ema20,
            "updated_at": datetime.utcnow().isoformat()
        }

        with open(EMA_FILE, "w") as f:
            json.dump(ema_data, f, indent=2)

        print(f"💾 Stored EMAs for {symbol} - 9EMA={ema9}, 20EMA={ema20}")
        log_to_file(f"Stored EMAs for {symbol} - 9EMA={ema9}, 20EMA={ema20}")
        return {"status": "ema stored"}

    # === Handle Trade Signal (with execution) ===
    elif data.get("action") in ("BUY", "SELL"):
        print(f"⚠️ Trade signal received: {data}")
        log_to_file(f"Trade signal received: {data}")

        symbol = data["symbol"]
        action = data["action"]
        quantity = int(data.get("quantity", 1))

        try:
            print(f"🐅 Sending order to TigerTrade: {symbol} {action} x{quantity}")
            result = subprocess.run([
                "python3", "execute_trade_live.py",
                symbol,
                action,
                str(quantity)
            ], capture_output=True, text=True)

            print("✅ TigerTrade stdout:", result.stdout)
            print("⚠️ TigerTrade stderr:", result.stderr)
            log_to_file(f"Executed TigerTrade: stdout={result.stdout.strip()} stderr={result.stderr.strip()}")

            # ✅ Load latest price from live_prices.json
            try:
                with open(PRICE_FILE, "r") as f:
                    prices = json.load(f)
                    price = float(prices.get(symbol, 0.0))
            except Exception as e:
                print(f"❌ Could not load price for {symbol}: {e}")
                log_to_file(f"Could not load price for {symbol}: {e}")
                price = 0.0

           # ✅ Append one row per contract to open_trades.csv
for _ in range(quantity):
    with open(OPEN_TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            symbol,      # e.g. MGC2508
            price,       # float, e.g. 2345.6
            action.upper(),  # BUY or SELL
            1,           # contracts_remaining (always 1 per row)
            1.0,         # trail_perc (default 1%)
            0.5,         # trail_offset (default 0.5%)
            "",          # tp_trail_price (placeholder until TP hit)
            "",          # ema9 placeholder
            ""           # ema20 placeholder
        ])
print("📥 Trade logged to open_trades.csv")
log_to_file(f"Trade logged to open_trades.csv: {symbol} {action} x{quantity} @ {price}")

            # ✅ Log to trade_log.json
            try:
                log_entry = {
                    "timestamp": datetime.utcnow().isoformat(),
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

                log_to_file(f"Trade logged to trade_log.json: {log_entry}")

            except Exception as e:
                print(f"❌ Failed to write to trade log: {e}")
                log_to_file(f"Failed to write to trade log: {e}")

        except Exception as e:
            print(f"❌ Failed to execute trade: {e}")
            log_to_file(f"Failed to execute trade: {e}")

        return {"status": "trade signal received"}

    # === Handle Trade Entry Only (No Tiger Execution) ===
    elif data.get("type") == "trade_entry":
        symbol = data["symbol"]
        price = data["price"]
        action = data["action"].upper()

        new_trade = {
            "symbol": symbol,
            "entry_price": price,
            "action": action,
            "contracts_remaining": 1,
            "trail_perc": 1.0,
            "trail_offset": 0.5,
            "tp_trail_price": "",
            "ema9": "",
            "ema20": ""
        }

        try:
            with open(OPEN_TRADES_FILE, "a", newline='') as file:
                writer = csv.DictWriter(file, fieldnames=new_trade.keys())
                writer.writerow(new_trade)
                log_to_file(f"✅ Logged new trade: {new_trade}")
        except Exception as e:
            print(f"❌ Failed to write trade to open_trades.csv: {e}")
            log_to_file(f"❌ Failed to write trade: {e}")

        return {"status": "trade logged"}

    return {"status": "unhandled alert type"}