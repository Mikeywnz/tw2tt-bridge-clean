import csv
import json
import time
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === FILE PATHS ===
PRICE_FILE = "live_prices.json"
EMA_FILE = "src/ema_values.json"
OPEN_TRADES_FILE = "open_trades.csv"
CLOSED_TRADES_FILE = "closed_trades.csv"

# === GOOGLE SHEETS CONFIG ===
SHEET_NAME = "Closed Trades Journal"  # Make sure this name matches exactly
GOOGLE_CREDS_FILE = "service_account.json"
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# === UTILITY LOADERS ===
def load_live_prices():
    with open(PRICE_FILE, 'r') as file:
        return json.load(file)

def load_ema_values():
    with open(EMA_FILE, 'r') as file:
        return json.load(file)

def load_open_trades():
    trades = []
    with open(OPEN_TRADES_FILE, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            trades.append({
                'symbol': row['symbol'],
                'entry_price': float(row['entry_price']),
                'action': row['action'].upper(),
                'contracts_remaining': int(row['contracts_remaining']),
                'trail_perc': float(row['trail_perc']),
                'trail_offset': float(row['trail_offset']),
                'tp_trail_price': float(row['tp_trail_price']) if row.get('tp_trail_price') else None,
                'ema9': None,
                'ema20': None
            })
    return trades

def write_closed_trade(trade, reason, exit_price):
    # === CALCULATE EXTRA FIELDS ===
    entry_price = trade['entry_price']
    direction = trade['action']
    pnl = (exit_price - entry_price) * (-1 if direction == "SELL" else 1)
    exit_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")  # You can adjust if tracking real entry times
    trail_triggered = "YES" if reason == "trailing_tp_exit" else ""
    ema9_cross_exit = "YES" if reason == "ema9_exit" else ""
    ema20_emergency_exit = "YES" if reason == "ema20_exit" else ""

    row = {
        "symbol": trade['symbol'],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "direction": direction,
        "reason_for_exit": reason,
        "pnl_dollars": round(pnl, 2),
        "entry_time": entry_time,
        "exit_time": exit_time,
        "trail_triggered": trail_triggered,
        "ema9_cross_exit": ema9_cross_exit,
        "ema20_emergency_exit": ema20_emergency_exit
    }

    # === WRITE TO CSV ===
    file_exists = False
    try:
        with open(CLOSED_TRADES_FILE, 'r') as f:
            file_exists = True
    except FileNotFoundError:
        pass

    with open(CLOSED_TRADES_FILE, 'a', newline='') as file:
        fieldnames = list(row.keys())
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    # === WRITE TO GOOGLE SHEETS ===
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, GOOGLE_SCOPE)
        gc = gspread.authorize(creds)
        sheet = gc.open(SHEET_NAME).sheet1
        sheet.append_row([
            row["symbol"],
            row["entry_price"],
            row["exit_price"],
            row["direction"],
            row["reason_for_exit"],
            row["pnl_dollars"],
            row["entry_time"],
            row["exit_time"],
            row["trail_triggered"],
            row["ema9_cross_exit"],
            row["ema20_emergency_exit"]
        ])
        print(f"✅ Trade written to Google Sheet: {row['symbol']} - {reason}")
    except Exception as e:
        print(f"❌ Failed to write to Google Sheet: {e}")

def write_remaining_trades(trades):
    with open(OPEN_TRADES_FILE, 'w', newline='') as file:
        fieldnames = ['symbol', 'entry_price', 'action', 'contracts_remaining', 'trail_perc', 'trail_offset', 'tp_trail_price', 'ema9', 'ema20']
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
                'tp_trail_price': t['tp_trail_price'] if t['tp_trail_price'] else '',
                'ema9': '',
                'ema20': ''
            })

# === MONITOR LOGIC ===
def monitor_trades():
    prices = load_live_prices()
    print("🟢 Loaded live_prices.json:", prices)
    ema_data = prices.get(symbol) or {}
    
    trades = load_open_trades()

    updated_trades = []
    for trade in trades:
        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1

        current_price = prices.get(symbol)
        ema9 = ema_data.get('ema9')
        ema20 = ema_data.get('ema20')

        if current_price is None or ema9 is None or ema20 is None:
            print(f"⏳ Skipping {symbol}: missing price or EMA data")
            updated_trades.append(trade)
            continue

        trade['ema9'] = ema9
        trade['ema20'] = ema20

        # === Emergency EMA20 cross exit ===
        if (trade['action'] == 'BUY' and current_price < ema20) or (trade['action'] == 'SELL' and current_price > ema20):
            print(f"🛑 Emergency EMA20 exit for {symbol} at {current_price}")
            write_closed_trade(trade, "ema20_exit", current_price)
            continue

        # === Momentum EMA9 close fade exit ===
        if (trade['action'] == 'BUY' and current_price < ema9) or (trade['action'] == 'SELL' and current_price > ema9):
            print(f"💨 Momentum EMA9 exit for {symbol} at {current_price}")
            write_closed_trade(trade, "ema9_exit", current_price)
            continue

        # === Trailing TP Logic ===
        trail_amount = trade['trail_perc'] / 100 * trade['entry_price']
        offset_amount = trade['trail_offset'] / 100 * trade['entry_price']
        trail_candidate = current_price - direction * offset_amount

        if trade['tp_trail_price'] is None:
            trade['tp_trail_price'] = trail_candidate
        elif direction * trail_candidate > direction * trade['tp_trail_price']:
            trade['tp_trail_price'] = trail_candidate

        if direction * current_price <= direction * trade['tp_trail_price']:
            print(f"📉 Trailing TP exit for {symbol} at {current_price}")
            write_closed_trade(trade, "trailing_tp_exit", current_price)
            continue

        # Keep trade
        updated_trades.append(trade)

    write_remaining_trades(updated_trades)

# === LOOP ===
if __name__ == "__main__":
    while True:
        monitor_trades()
        time.sleep(10)