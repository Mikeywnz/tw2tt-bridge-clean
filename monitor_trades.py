import os
import csv
import json
import time

# === PATH SETUP: Use absolute paths ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
open_trades_path = os.path.join(BASE_DIR, "open_trades.csv")
live_prices_path = os.path.join(BASE_DIR, "live_prices.json")

# === FUNCTION: Load current open trades from CSV ===
def load_open_trades():
    trades = []
    if os.path.exists(open_trades_path):
        with open(open_trades_path, "r") as file:
            reader = csv.DictReader(file)
            for row in reader:
                trades.append({
                    "symbol": row['symbol'],
                    "entry_price": float(row['entry_price']),
                    "tp_price": float(row['tp_price']),
                    "sl_price": float(row['sl_price']),
                    "direction": row['action'].upper()
                })
    return trades

# === FUNCTION: Load latest live prices ===
def load_live_prices():
    if os.path.exists(live_prices_path):
        with open(live_prices_path, "r") as f:
            return json.load(f)
    return {}

# === FUNCTION: Monitor trades and evaluate TP/SL ===
def monitor_trades():
    print("🔍 Starting trade monitoring loop...")
    while True:
        open_trades = load_open_trades()
        live_prices = load_live_prices()

        for trade in open_trades:
            symbol = trade['symbol']
            price = live_prices.get(symbol)

            if price is None:
                print(f"⚠️  No live price for {symbol}")
                continue

            if trade['direction'] == "BUY":
                if price >= trade['tp_price']:
                    print(f"✅ TP hit for {symbol} at {price}")
                elif price <= trade['sl_price']:
                    print(f"🛑 SL hit for {symbol} at {price}")
            elif trade['direction'] == "SELL":
                if price <= trade['tp_price']:
                    print(f"✅ TP hit for {symbol} at {price}")
                elif price >= trade['sl_price']:
                    print(f"🛑 SL hit for {symbol} at {price}")

        time.sleep(10)  # Wait 10 seconds before next check

# === MAIN ENTRYPOINT ===
if __name__ == "__main__":
    print("📂 Loaded open trades")
    monitor_trades()