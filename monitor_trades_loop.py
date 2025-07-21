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
        print(f"\U0001f4e4 Exit order sent: {exit_action} 1 {symbol}")
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
CLOSED_TRADES_FILE = "closed_trades.csv"
GOOGLE_CREDS_FILE = "service_account.json"
SHEET_NAME = "Closed Trades Journal"
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"

# === Load live prices from Firebase ===
def load_live_prices():
    return db.reference("live_prices").get() or {}

# === Write closed trade to CSV + Google Sheets ===
def write_closed_trade(trade, reason, exit_price):
    pnl = (exit_price - trade['entry_price']) * (1 if trade['action'] == 'BUY' else -1)
    now_nz = datetime.now(timezone("Pacific/Auckland"))
    exit_time = now_nz.strftime("%Y-%m-%d %H:%M:%S")
    day_date = now_nz.strftime("%A %d %B %Y")  # e.g., "Monday 21 July 2025"
    order_id = trade.get("order_id", "")

    # 🟢 Friendly reason label mapping
    reason_map = {
        "trailing_tp_exit": "Trailing Take Profit",
        "manual_close": "Manual Close",
        "ema_flattening_exit": "EMA Flattening",
        "liquidation": "Liquidation",
        "LACK_OF_MARGIN": "Lack of Margin",
        "FILLED": "FILLED",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Lack of Margin"
    }
    friendly_reason = reason_map.get(reason, reason)

    row = {
        "day_date": day_date,  
        "symbol": trade["symbol"],
        "direction": trade["action"],
        "entry_price": trade["entry_price"],
        "exit_price": exit_price,
        "pnl_dollars": round(pnl, 2),
        "reason_for_exit": friendly_reason,
        "entry_time": trade.get("entry_timestamp"),
        "exit_time": exit_time,
        "trail_triggered": "YES" if trade.get("trail_hit") else "NO"
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
        sheet = gc.open_by_key(SHEET_ID).worksheet("Sheet1")
        day_date = now_nz.strftime("%A %d %B %Y")  # e.g., Monday 21 July 2025
        sheet.append_row([
            day_date,
            row["symbol"],
            row["direction"],
            row["entry_price"],
            row["exit_price"],
            row["pnl_dollars"],
            friendly_reason,
            row["entry_time"],
            row["exit_time"],
            row["trail_triggered"],
            row["order_id"]
        ])
        print(f"✅ Logged to Google Sheet: {row['symbol']} – {friendly_reason}")
    except Exception as e:
        import traceback
        print(f"❌ Google Sheets error for {trade['symbol']}: {e}")
        traceback.print_exc()

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
    try:
        seen_ids = set()
        for t in trades:
            if "trade_id" not in t:
                continue
            seen_ids.add(t["trade_id"])
            trade_id = t["trade_id"]
            firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_trades/MGC2508/{trade_id}.json"
            requests.put(firebase_url, json=t).raise_for_status()
            print(f"✅ Saved trade {trade_id} to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

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

def load_trailing_tp_settings():
    try:
        fb_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/trailing_tp_settings.json"
        res = requests.get(fb_url)
        cfg = res.json() if res.ok else {}
        if cfg.get("enabled", False):
            return float(cfg.get("trigger_points", 14.0)), float(cfg.get("offset_points", 5.0))
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0

# === MONITOR LOOP ===
exit_in_progress = set()
GRACE_PERIOD_SECONDS = 30

def monitor_trades():
    trigger_points, offset_points = load_trailing_tp_settings()
    current_time = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    if current_time - monitor_trades.last_heartbeat >= 60:
        mgc_price = load_live_prices().get("MGC2508", {}).get('price')
        print(f"🛰️ System working – MGC2508 price: {mgc_price}")
        monitor_trades.last_heartbeat = current_time

    all_trades = load_open_trades()

    for t in all_trades:
        tid = t.get('trade_id', 'unknown')
        if not t.get('filled'):
            print(f"⏭️ Skipping {tid} – not filled")
        if t.get('contracts_remaining', 0) <= 0:
            print(f"⏭️ Skipping {tid} – no contracts remaining")

    active_trades = []
    for t in all_trades:
        if not t or not isinstance(t, dict):
            continue
        tid = t.get('trade_id', 'unknown')
        if t.get('exited') or t.get('status') == 'failed':
            continue
        if not t.get('filled') or t.get('contracts_remaining', 0) <= 0:
            continue
        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

    updated_trades = []
    prices = load_live_prices()

    for trade in active_trades:
        if not trade or not isinstance(trade, dict):
            continue
        trade_id = trade.get('trade_id', 'unknown')
        print(f"🔄 Processing trade {trade_id}")
        if trade.get('exited') or trade_id in exit_in_progress:
            print(f"⏭️ Skipping already exited/in-progress trade {trade_id}")
            continue

        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {trade_id}")
            updated_trades.append(trade)
            continue

        entry = trade['entry_price']
        if entry <= 0:
            print(f"❌ Invalid entry price for {trade_id} — skipping.")
            continue

        tp_trigger = trigger_points

        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            if (direction == 1 and current_price >= entry + tp_trigger) or (direction == -1 and current_price <= entry - tp_trigger):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"🎯 TP trigger hit for {trade_id} → trail activated at {current_price}")

        if trade.get('trail_hit'):
            if (direction == 1 and current_price > trade['trail_peak']) or (direction == -1 and current_price < trade['trail_peak']):
                trade['trail_peak'] = current_price
            buffer_amt = offset_points
            if (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt):
                print(f"🚨 Trailing TP exit for {trade_id}: price={current_price}, peak={trade['trail_peak']}")
                exit_in_progress.add(trade_id)
                close_position(symbol, trade['action'])
                write_closed_trade(trade, 'trailing_tp_exit', current_price)
                try:
                    success = delete_trade_from_firebase(trade_id)
                    if success:
                        print(f"✅ Trade {trade_id} successfully deleted.")
                    else:
                        print(f"❌ Trade {trade_id} still exists in Firebase after attempted delete.")
                except Exception as e:
                    import traceback
                    print(f"❌ Error while deleting trade {trade_id} from Firebase: {e}")
                    traceback.print_exc()
                trade['exited'] = True
                continue

        updated_trades.append(trade)

    save_open_trades(updated_trades)

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)