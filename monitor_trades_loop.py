import os
import csv
import json
import time

# === PATH SETUP ===
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
                trades.append(row)
    return trades

# === FUNCTION: Load live prices ===
def load_live_prices():
    if os.path.exists(live_prices_path):
        with open(live_prices_path, "r") as f:
            return json.load(f)
    return {}

# === FUNCTION: Save updated trades back to CSV ===
def save_open_trades(trades):
    if not trades:
        return
    with open(open_trades_path, "w", newline='') as file:
        writer = csv.DictWriter(file, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)

# === FUNCTION: Monitor trades and evaluate TP/SL ===
def monitor_trades():
    print("🔁 Starting trade monitoring loop...")
    while True:
        trades = load_open_trades()
        prices = load_live_prices()

        updated_trades = []
        for trade in trades:
            symbol = trade["symbol"]
            action = trade["action"].upper()
            price = prices.get(symbol)

            if price is None:
                print(f"⚠️ No price for {symbol}")
                updated_trades.append(trade)
                continue

            entry = float(trade["entry_price"])
            tp = float(trade["tp_price"])
            sl = float(trade["sl_price"])
            contracts = int(trade["contracts_remaining"])

            # Convert flags
            pt1 = trade.get("partial_tp1", "false").lower() == "true"
            pt2 = trade.get("partial_tp2", "false").lower() == "true"
            pt3 = trade.get("partial_tp3", "false").lower() == "true"

            if action == "BUY":
                if not pt1 and price >= entry + 5:
                    print(f"🎯 TP1 HIT for {symbol} at {price} (SELL 1)")
                    contracts -= 1
                    pt1 = True
                elif not pt2 and price >= entry + 10:
                    print(f"🎯 TP2 HIT for {symbol} at {price} (SELL 1)")
                    contracts -= 1
                    pt2 = True
                elif not pt3 and price >= tp:
                    print(f"🎯 FINAL TP HIT for {symbol} at {price} (SELL 1)")
                    contracts -= 1
                    pt3 = True
                elif price <= sl:
                    print(f"🛑 STOP LOSS HIT for {symbol} at {price} (SELL ALL)")
                    contracts = 0

            elif action == "SELL":
                if not pt1 and price <= entry - 5:
                    print(f"🎯 TP1 HIT for {symbol} at {price} (BUY 1)")
                    contracts -= 1
                    pt1 = True
                elif not pt2 and price <= entry - 10:
                    print(f"🎯 TP2 HIT for {symbol} at {price} (BUY 1)")
                    contracts -= 1
                    pt2 = True
                elif not pt3 and price <= tp:
                    print(f"🎯 FINAL TP HIT for {symbol} at {price} (BUY 1)")
                    contracts -= 1
                    pt3 = True
                elif price >= sl:
                    print(f"🛑 STOP LOSS HIT for {symbol} at {price} (BUY ALL)")
                    contracts = 0

            # Skip adding back if all contracts sold
            if contracts <= 0:
                print(f"✅ POSITION CLOSED for {symbol}")
                continue

            # Update trade
            trade["contracts_remaining"] = str(contracts)
            trade["partial_tp1"] = str(pt1).lower()
            trade["partial_tp2"] = str(pt2).lower()
            trade["partial_tp3"] = str(pt3).lower()
            updated_trades.append(trade)

        # Save any changes
        save_open_trades(updated_trades)
        time.sleep(10)

# === MAIN ENTRY ===
if __name__ == "__main__":
    monitor_trades()
