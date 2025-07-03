import os
import csv
import json
import time

# Get absolute path to current folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Absolute paths for data files
open_trades_path = os.path.join(BASE_DIR, "open_trades.csv")
live_prices_path = os.path.join(BASE_DIR, "live_prices.json")

print("✅ Trade monitor started...")

while True:
    # === STEP 1: LOAD OPEN TRADES ===
    open_trades = []
    try:
        with open(open_trades_path, "r") as file:
            reader = csv.DictReader(file)
            for row in reader:
                open_trades.append({
                    "symbol": row["symbol"],
                    "entry_price": float(row["entry_price"]),
                    "tp_price": float(row["tp_price"]),
                    "sl_price": float(row["sl_price"]),
                    "direction": row["action"].upper()
                })
        print("📘 Loaded open trades")
    except Exception as e:
        print(f"❌ Error reading open_trades.csv: {e}")

    # === STEP 2: READ LIVE PRICES ===
    try:
        with open(live_prices_path, "r") as f:
            prices = json.load(f)
            print(f"📊 Live prices received: {prices}")
    except Exception as e:
        print(f"❌ Error reading live_prices.json: {e}")

    # === STEP 3: CHECK TP/SL (You can expand this logic next) ===
    print("🔁 Checking trades against price...")

    time.sleep(10)