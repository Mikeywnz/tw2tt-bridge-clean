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

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        print(f"❌ Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    print(f"📥 Incoming Webhook: {data}")

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

        print(f"💾 Stored EMAs for {symbol} — 9EMA={ema9}, 20EMA={ema20}")
        return {"status": "ema stored"}

    # === Handle Trade Signal ===
    elif data.get("action") in ("BUY", "SELL"):
        print(f"⚠️ Trade signal received: {data}")

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

            # === Try to load live price for logging
            try:
                with open(PRICE_FILE, "r") as f:
                    prices = json.load(f)
                    entry_price = prices.get(symbol, None)
            except:
                entry_price = None

            if entry_price is not None:
                for _ in range(quantity):
                    row = [
                        symbol,
                        entry_price,
                        action,
                        1,      # contracts_remaining
                        1.0,    # trail_perc
                        0.5,    # trail_offset
                        "",     # tp_trail_price
                        "",     # ema9_exit_active
                        ""      # ema20_exit_active
                    ]
                    file_exists = os.path.isfile(OPEN_TRADES_FILE)
                    with open(OPEN_TRADES_FILE, "a", newline="") as csvfile:
                        writer = csv.writer(csvfile)
                        if not file_exists or os.path.getsize(OPEN_TRADES_FILE) == 0:
                            writer.writerow([
                                "symbol", "entry_price", "action", "contracts_remaining",
                                "trail_perc", "trail_offset", "tp_trail_price",
                                "ema9_exit_active", "ema20_exit_active"
                            ])
                        writer.writerow(row)
                    print(f"📈 Trade logged to open_trades.csv: {row}")
            else:
                print("⚠️ No live price available to log trade.")

        except Exception as e:
            print(f"❌ Failed to execute trade: {e}")

        return {"status": "trade executed and logged"}

    return {"status": "unhandled alert type"}