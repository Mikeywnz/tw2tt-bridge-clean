import firebase_admin
from firebase_admin import credentials, db
import csv
import time
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

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

def load_open_trades():
    trades = []
    with open(OPEN_TRADES_FILE, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row.get("filled", "true") != "true":
                continue
            trades.append({
                'symbol': row['symbol'],
                'entry_price': float(row['entry_price']),
                'action': row['action'].upper(),
                'contracts_remaining': int(row['contracts_remaining']),
                'trail_perc': float(row['trail_perc']),
                'trail_offset': float(row['trail_offset']),
                'tp_trail_price': float(row.get('tp_trail_price') or 0),
                'trail_hit': row.get('trail_hit', 'false') == 'true',
                'trail_peak': float(row.get('trail_peak') or row['entry_price']),
                'ema9': None,
                'ema20': None,
                'filled': row.get('filled', 'true'),
                'entry_timestamp': row.get('entry_timestamp', '')
            })
    return trades

def write_closed_trade(trade, reason, exit_price):
    pnl = (exit_price - trade['entry_price']) * (-1 if trade['action'] == 'SELL' else 1)
    exit_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry_time = trade.get("entry_timestamp", exit_time)

    exit_color = {
        "trailing_tp_exit": "Green",
        "ema9_exit": "Blue",
        "ema20_exit": "Red",
        "manual_exit": "Orange",
        "liquidated": "Purple",
        "ghost_trade_exit": "Grey"
    }.get(reason, "")

    row = {
        "symbol": trade['symbol'],
        "entry_price": trade['entry_price'],
        "exit_price": exit_price,
        "direction": trade['action'],
        "reason_for_exit": reason,
        "pnl_dollars": round(pnl, 2),
        "entry_time": entry_time,
        "exit_time": exit_time,
        "trail_triggered": "YES" if reason == "trailing_tp_exit" else "",
        "ema9_cross_exit": "YES" if reason == "ema9_exit" else "",
        "ema20_emergency_exit": "YES" if reason == "ema20_exit" else "",
        "exit_color": exit_color
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
        print(f"❌ Google Sheets error: {e}")

def write_remaining_trades(trades):
    with open(OPEN_TRADES_FILE, 'w', newline='') as file:
        fieldnames = ['symbol', 'entry_price', 'action', 'contracts_remaining', 'trail_perc', 'trail_offset', 'tp_trail_price', 'trail_hit', 'trail_peak', 'ema9_live', 'ema20_live', 'filled', 'entry_timestamp']
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow({
                'symbol': t['symbol'],
                'entry_price': t['entry_price'],
                'action': t['action'],
                'contracts_remaining': t['contracts_remaining'],
                'trail_perc': t['trail_perc'],
                'trail_offset': t['trail_offset'],
                'tp_trail_price': t.get('tp_trail_price', ''),
                'trail_hit': str(t.get('trail_hit', False)).lower(),
                'trail_peak': t.get('trail_peak', ''),
                'ema9_live': t.get('ema9_live', ''),
                'ema20_live': t.get('ema20_live', ''),
                'filled': t.get('filled', 'true'),
                'entry_timestamp': t.get('entry_timestamp', '')
            })

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
            ema9 = symbol_data.get('ema9')
            ema20 = symbol_data.get('ema20')
        else:
            current_price = symbol_data
            ema9 = None
            ema20 = None

        if current_price is None or ema9 is None or ema20 is None:
            print(f"⏳ Skipping {symbol}: missing price or EMA")
            updated_trades.append(trade)
            continue

        trade['ema9_live'] = ema9
        trade['ema20_live'] = ema20

        if (trade['action'] == 'BUY' and current_price < ema20) or (trade['action'] == 'SELL' and current_price > ema20):
            print(f"🛑 EMA20 exit: {symbol} at {current_price}")
            write_closed_trade(trade, "ema20_exit", current_price)
            continue

        if (trade['action'] == 'BUY' and current_price < ema9) or (trade['action'] == 'SELL' and current_price > ema9):
            print(f"💨 EMA9 exit: {symbol} at {current_price}")
            write_closed_trade(trade, "ema9_exit", current_price)
            continue

        # === Trailing TP logic ===
        entry = trade['entry_price']
        tp_trigger_pct = trade['trail_perc']
        trail_buffer_pct = trade['trail_offset']
        tp_trigger = entry * tp_trigger_pct / 100

        if not trade.get('trail_hit') and (
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
                write_closed_trade(trade, "trailing_tp_exit", current_price)
                continue

        # === Ghost trade detection === (placeholder: price == -1)
        if current_price == -1:
            print(f"👻 Ghost trade detected: {symbol} — no longer live in TigerTrade.")
            write_closed_trade(trade, "ghost_trade_exit", trade['entry_price'])
            continue

        updated_trades.append(trade)

    write_remaining_trades(updated_trades)

if __name__ == "__main__":
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)