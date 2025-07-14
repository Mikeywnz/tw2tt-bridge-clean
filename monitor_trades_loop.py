# === MONITOR_TRADES_LOOP.PY (Updated) ===
import firebase_admin
from firebase_admin import credentials, db
import csv
import time
from datetime import datetime
from pytz import timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests 
import subprocess

# === Helper to execute exit trades ===
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

# === File paths & Sheets config ===
OPEN_TRADES_FILE = "open_trades.csv"
CLOSED_TRADES_FILE = "closed_trades.csv"
GOOGLE_CREDS_FILE = "service_account.json"
SHEET_NAME = "Closed Trades Journal"
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# === Load live prices from Firebase ===
def load_live_prices():
    return db.reference("live_prices").get() or {}

# === Write closed trade to CSV + Google Sheets ===
def write_closed_trade(trade, reason, exit_price):
    pnl = (exit_price - trade['entry_price']) * (1 if trade['action'] == 'BUY' else -1)
    exit_time = datetime.now(timezone("Pacific/Auckland")).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "symbol": trade["symbol"],
        "direction": trade["action"],
        "entry_price": trade["entry_price"],
        "exit_price": exit_price,
        "pnl_dollars": round(pnl, 2),
        "reason_for_exit": reason,
        "entry_time": trade.get("entry_timestamp"),
        "exit_time": exit_time,
        "trail_triggered": "YES" if trade.get("trail_hit") else "NO"
    }
    # Append to closed_trades.csv
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
    # Append to Google Sheets
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, GOOGLE_SCOPE)
        gc = gspread.authorize(creds)
        sheet = gc.open(SHEET_NAME).sheet1
        sheet.append_row(list(row.values()))
        print(f"✅ Logged to Google Sheet: {row['symbol']} – {reason}")
    except Exception as e:
        print(f"❌ Google Sheets error for {trade['symbol']}: {e}")

# === Firebase open trades handlers ===
def load_open_trades():
    firebase_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508.json"
    try:
        resp = requests.get(firebase_url)
        resp.raise_for_status()
        data = resp.json() or {}
        trades = []
        if isinstance(data, dict):
            for tid, td in data.items():
                td['trade_id'] = tid
                trades.append(td)
        else:
            trades = data
        print(f"🔄 Loaded {len(trades)} open trades from Firebase.")
        return trades
    except Exception as e:
        print(f"❌ Failed to fetch open trades: {e}")
        return []

def save_open_trades(trades):
    firebase_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508.json"
    try:
        requests.put(firebase_url, json=trades).raise_for_status()
        print(f"✅ Saved {len(trades)} open trades to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# === Delete trade from Firebase ===
def delete_trade_from_firebase(trade_id):
    firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508/{trade_id}.json"
    try:
        resp = requests.delete(firebase_url)
        resp.raise_for_status()
        print(f"✅ Deleted trade {trade_id} from Firebase.")
        return True
    except Exception as e:
        print(f"❌ Failed to delete trade {trade_id} from Firebase: {e}")
        return False

# === MONITOR LOOP ===
def monitor_trades():
    current_time = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    # Heartbeat log every 60s
    if current_time - monitor_trades.last_heartbeat >= 60:
        mgc_price = load_live_prices().get("MGC2508", {}).get('price')
        print(f"🛰️ System working – MGC2508 price: {mgc_price}")
        monitor_trades.last_heartbeat = current_time

    all_trades = load_open_trades()
    # Filter active, filled trades and skip any with invalid trailing values
    active_trades = []
    for t in all_trades:
        tid = t.get('trade_id', 'unknown')
        if not t.get('filled') or t.get('contracts_remaining', 0) <= 0:
            continue
        tp_pct = t.get('trail_trigger', 0)
        buf_pct = t.get('trail_offset', 0)
        if tp_pct < 0.01 or buf_pct < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP settings: trigger={tp_pct}, buffer={buf_pct}")
            continue
        active_trades.append(t)
    if not active_trades:
        print("⚠️ No active trades found — worker is still awake.")

    updated_trades = []
    prices = load_live_prices()

    for trade in active_trades:
        trade_id = trade.get('trade_id', 'unknown')
        print(f"🔄 Processing trade {trade_id}")
        # Skip if already marked exited in-memory
        if trade.get('exited'):
            print(f"⏭️ Skipping already exited trade {trade_id}")
            continue
        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)
        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {trade_id}")
            updated_trades.append(trade)
            continue

        entry = trade['entry_price']
        tp_trigger = entry * trade['trail_trigger'] / 100
        # === TP Trigger Activation ===
        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            if (direction == 1 and current_price >= entry + tp_trigger) or (direction == -1 and current_price <= entry - tp_trigger):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"🎯 TP trigger hit for {trade_id} → trail activated at {current_price}")

        # === Trailing TP Exit ===
        if trade.get('trail_hit'):
            # Update peak
            if (direction == 1 and current_price > trade['trail_peak']) or (direction == -1 and current_price < trade['trail_peak']):
                trade['trail_peak'] = current_price
            buffer_amt = trade['trail_peak'] * trade['trail_offset'] / 100
            # Check exit condition
            if (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt):
                print(f"🚨 Trailing TP exit for {trade_id}: price={current_price}, peak={trade['trail_peak']}")
                close_position(symbol, trade['action'])
                write_closed_trade(trade, 'trailing_tp_exit', current_price)
                # Attempt deletion
                success = delete_trade_from_firebase(trade_id)
                # Mark exited in-memory to prevent any retry this run
                trade['exited'] = True
                # If deletion failed, do not re-add to updated_trades; if succeeded, it's already removed
                continue

        # === Ghost trade guard ===
        if current_price == -1:
            write_closed_trade(trade, 'ghost_trade_exit', trade['entry_price'])
            delete_trade_from_firebase(trade_id)
            continue

        # Keep trade if still active
        updated_trades.append(trade)

    # === Always persist current open trades list back to Firebase ===
    save_open_trades(updated_trades)

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)
