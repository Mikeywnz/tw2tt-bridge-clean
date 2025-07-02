import csv
import time
import json

# === CONFIG ===
TRADES_CSV = 'open_trades.csv'
LIVE_PRICES_FILE = 'live_prices.json'
CHECK_INTERVAL = 10  # seconds

# === LOAD OPEN TRADES ===
def load_open_trades():
    trades = []
    try:
        with open(TRADES_CSV, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                trades.append({
                    "symbol": row['symbol'],
                    "entry_price": float(row['entry_price']),
                    "tp_price": float(row['tp_price']),
                    "sl_price": float(row['sl_price']),
                    "direction": row['action'].upper()
                })
    except Exception as e:
        print(f"❌ Error reading open_trades.csv: {e}")
    return trades

# === GET PRICE FROM live_prices.json ===
def get_live_price(symbol):
    try:
        with open(LIVE_PRICES_FILE, 'r') as file:
            prices = json.load(file)
            if symbol in prices:
                return float(prices[symbol])
            else:
                print(f"⚠️ No live price found for {symbol}")
                return None
    except Exception as e:
        print(f"❌ Error reading live_prices.json: {e}")
        return None

# === MONITOR TRADES ===
def monitor_trades():
    print("🟢 Trade monitor started.")
    while True:
        trades = load_open_trades()
        for trade in trades:
            symbol = trade['symbol']
            direction = trade['direction']
            entry = trade['entry_price']
            tp = trade['tp_price']
            sl = trade['sl_price']

            price = get_live_price(symbol)
            if price is None:
                continue

            print(f"📈 {symbol} | Price: {price} | TP: {tp} | SL: {sl}")

            # Exit logic
            if direction == "BUY":
                if price >= tp:
                    print(f"✅ TAKE PROFIT hit for {symbol} (BUY). Price: {price}")
                elif price <= sl:
                    print(f"🛑 STOP LOSS hit for {symbol} (BUY). Price: {price}")
            elif direction == "SELL":
                if price <= tp:
                    print(f"✅ TAKE PROFIT hit for {symbol} (SELL). Price: {price}")
                elif price >= sl:
                    print(f"🛑 STOP LOSS hit for {symbol} (SELL). Price: {price}")
            else:
                print(f"⚠️ Unknown direction {direction} for {symbol}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    monitor_trades()