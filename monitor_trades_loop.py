import firebase_admin
from firebase_admin import credentials, db
import csv
import time
from datetime import datetime
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests 
import subprocess  # Add this if it's not already at the top

def close_position(symbol, original_action):
    exit_action = "SELL" if original_action == "BUY" else "BUY"
    try:
        result = subprocess.run(
            ["python3", "execute_trade_live.py", symbol, exit_action, "1"],
            capture_output=True,
            text=True
        )
        print(f"📤 Exit order sent: {exit_action} 1 {symbol}")
        print("stdout:", result.stdout.strip())
        print("stderr:", result.stderr.strip())
    except Exception as e:
        print(f"❌ Failed to execute exit order: {e}")

# === FIREBASE INITIALIZATION ===
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase_key.json")
    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/"
    })

# === FILE PATHS ===
OPEN_TRADES_FILE = "open_trades.csv"
CLOSED_TRADES_FILE = "closed_trades.csv"
GOOGLE_CREDS_FILE = "service_account.json"
SHEET_NAME = "Closed Trades Journal"
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# === LOADERS ===
def load_live_prices():
    return db.reference("live_prices").get() or {}

def write_closed_trade(trade, reason, exit_price):
    pnl = (exit_price - trade['entry_price']) * (-1 if trade['action'] == 'SELL' else 1)
    exit_time = datetime.now(timezone("Pacific/Auckland")).strftime("%Y-%m-%d %H:%M:%S")
    entry_time = trade.get(
        "entry_timestamp",
        datetime.now(timezone("Pacific/Auckland")).strftime("%Y-%m-%d %H:%M:%S")
    )
    
    exit_color = {
        "trade_id": trade.get("trade_id", ""),
        "trailing_tp_exit": "Green",
        "ema50_exit": "Red",
        "manual_exit": "Orange",
        "liquidated": "Purple",
        "ghost_trade_exit": "Grey"
    }.get(reason, "")

    row = {
        "symbol": trade["symbol"],
        "direction": trade["action"],
        "entry_price": trade["entry_price"],
        "exit_price": exit_price,
        "pnl_dollars": round((exit_price - trade["entry_price"]) * (1 if trade["action"] == "BUY" else -1), 2),
        "reason_for_exit": reason,
        "entry_time": trade["entry_timestamp"],
        "exit_time": datetime.now(timezone('Pacific/Auckland')).isoformat(),
        "trail_triggered": "YES" if trade.get("trail_hit") else "NO",
        "ema50_exit": "YES" if reason == "ema50_exit" else "NO"
    }

    try:
        file_exists = False
        with open(CLOSED_TRADES_FILE, 'r') as f:
            file_exists = True
    except FileNotFoundError:
        pass

    with open(CLOSED_TRADES_FILE, 'a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, GOOGLE_SCOPE)
        gc = gspread.authorize(creds)
        sheet = gc.open(SHEET_NAME).sheet1
        sheet.append_row(list(row.values()))
        print(f"✅ Logged to Google Sheet: {row['symbol']} – {reason}")
    except Exception as e:
        log_line = f"❌ Google Sheets error for {trade['symbol']}: {e}"
        print(log_line)

# === Load open trades from Firebase instead of CSV ===
def load_open_trades():
    firebase_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508.json"
    try:
        resp = requests.get(firebase_url)
        resp.raise_for_status()
        data = resp.json() or {}
        trades = []
        if isinstance(data, dict):
            # Firebase returns { trade_id: trade_data, … }
            for tid, td in data.items():
                td["trade_id"] = tid
                trades.append(td)
        else:
            # in case it's already a list
            trades = data
        return trades
    except Exception as e:
        print(f"❌ Failed to fetch open trades: {e}")
        return []

# === Save open trades to Firebase ===
def save_open_trades(trades):
    firebase_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508.json"
    try:
        response = requests.put(firebase_url, json=trades)
        response.raise_for_status()
        print("✅ Open trades saved to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# === Delete trade from Firebase ===
def delete_trade_from_firebase(trade_id):
    firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508.json"
    try:
        response = requests.delete(firebase_url)
        if response.status_code == 200:
            print(f"✅ Deleted trade {trade_id} from Firebase.")
        else:
            print(f"❌ Failed to delete trade {trade_id} from Firebase: {response.text}")
    except Exception as e:
        print(f"❌ Error deleting trade {trade_id} from Firebase: {e}")

# === MONITOR LOOP ===
def monitor_trades():
    prices = load_live_prices()
    print("🟢 Prices loaded:", prices)

    trades = load_open_trades()
    updated_trades = []

    for trade in trades:
        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        symbol_data = prices.get(symbol, {})

        if isinstance(symbol_data, dict):
            current_price = symbol_data.get('price')
            ema50 = symbol_data.get('ema50')
        else:
            current_price = symbol_data
            ema50 = None

        if current_price is None or ema50 is None:
            print(f"⏳ Skipping {symbol}: missing price or ema50")
            updated_trades.append(trade)
            continue

        trade['ema50_live'] = ema50

        # === 50EMA Emergency Exit ===
        if (trade['action'] == 'BUY' and current_price < ema50) or (trade['action'] == 'SELL' and current_price > ema50):
            print(f"🛑 EMA50 exit: {symbol} at {current_price}")
            close_position(symbol, trade["action"])
            write_closed_trade(trade, "ema50_exit", current_price)
            delete_trade_from_firebase(trade.get("trade_id", ""))
            continue

        # === Trailing TP logic ===
        entry = trade['entry_price']
        tp_trigger_pct = trade['trail_trigger']
        trail_buffer_pct = trade['trail_offset']
        tp_trigger = entry * tp_trigger_pct / 100

        if not trade.get('trail_hit'):
            if trade.get('trail_peak') is None:
                trade['trail_peak'] = entry
            if (
                (direction == 1 and current_price >= entry + tp_trigger) or
                (direction == -1 and current_price <= entry - tp_trigger)
            ):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"🎯 TP trigger hit for {symbol} → trailing activated at {current_price}")

        if trade.get('trail_hit'):
            if direction == 1 and current_price > trade.get('trail_peak', entry):
                trade['trail_peak'] = current_price
            elif direction == -1 and current_price < trade.get('trail_peak', entry):
                trade['trail_peak'] = current_price

            trail_buffer = trade['trail_peak'] * trail_buffer_pct / 100

            if (
                (direction == 1 and current_price <= trade['trail_peak'] - trail_buffer) or
                (direction == -1 and current_price >= trade['trail_peak'] + trail_buffer)
            ):
                print(f"🚨 Trailing TP exit: {symbol} at {current_price} (peak was {trade['trail_peak']})")
                close_position(symbol, trade["action"])
                write_closed_trade(trade, "trailing_tp_exit", current_price)
                continue

        # === Ghost trade detection ===
        if current_price == -1:
            print(f"👻 Ghost trade detected: {symbol} — no longer live in TigerTrade.")
            write_closed_trade(trade, "ghost_trade_exit", trade['entry_price'])
            continue

        updated_trades.append(trade)

    save_open_trades(updated_trades)

if __name__ == "__main__":
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)