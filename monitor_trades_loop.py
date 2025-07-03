import os
import csv
import json
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
open_trades_path = os.path.join(BASE_DIR, "open_trades.csv")
live_prices_path = os.path.join(BASE_DIR, "live_prices.json")

# === Load trades from CSV ===
def load_open_trades():
    trades = []
    with open(open_trades_path, "r") as file:
        reader = csv.DictReader(file)
        for row in reader:
            trades.append(row)
    return trades

# === Save updated trades back to CSV ===
def save_open_trades(trades):
    if not trades:
        open(open_trades_path, "w").close()
        return
    with open(open_trades_path, "w", newline='') as file:
        writer = csv.DictWriter(file, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)

# === Load latest prices ===
def load_live_prices():
    if os.path.exists(live_prices_path):
        with open(live_prices_path, "r") as f:
            return json.load(f)
    return {}

# === Main monitor loop ===
def monitor_trades():
    print("🔁 Monitoring trades for partial TPs...")
    while True:
        trades = load_open_trades()
        prices = load_live_prices()
        updated_trades = []

        for trade in trades:
            symbol = trade["symbol"]
            price = prices.get(symbol)

            if not price:
                print(f"⚠️  No price for {symbol}")
                updated_trades.append(trade)
                continue

            direction = trade["action"].upper()
            contracts = int(trade["contracts_remaining"])
            hit = False

            for level in ["partial_tp1", "partial_tp2", "partial_tp3"]:
                if trade[level].lower() == "false":
                    continue
                tp = float(trade[level])
                if direction == "BUY" and float(price) >= tp:
                    print(f"✅ {symbol}: Hit {level} at {price}, selling 1 contract")
                    contracts -= 1
                    trade[level] = "false"
                    hit = True
                    break  # Only 1 hit per loop

            trade["contracts_remaining"] = str(contracts)

            if contracts > 0:
                updated_trades.append(trade)
            else:
                print(f"🎯 {symbol}: All contracts closed")

        save_open_trades(updated_trades)
        time.sleep(10)

# === Start ===
if __name__ == "__main__":
    print("📂 Loaded open trades")
    monitor_trades()