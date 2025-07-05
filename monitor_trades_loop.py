import csv
import time
import json
from datetime import datetime, timedelta

# Load live prices from TradingView updates
def load_live_prices():
    with open('live_prices.json', 'r') as file:
        return json.load(file)

# Load open trades
def load_open_trades():
    trades = []
    with open('open_trades.csv', 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            trade = {
                'symbol': row['symbol'],
                'entry_price': float(row['entry_price']),
                'tp1_mult': float(row['tp1_mult']),
                'tp2_mult': float(row['tp2_mult']),
                'tp3_mult': float(row['tp3_mult']),
                'sl_price': float(row['sl_price']),
                'action': row['action'].upper(),
                'contracts_remaining': int(row['contracts_remaining']),
                'trailing_tp_active': row['trailing_tp_active'].lower() == 'true',
                'trail_offset': float(row['trail_offset']),
                'be_offset': float(row['be_offset']),
                'trail_timeout': int(row['trail_timeout']),
                'tp_hit_stage': 0,
                'tp_trail_price': None,
                'tp_timeout_start': None
            }
            trades.append(trade)
    return trades

# Main monitor loop
def monitor_trades():
    trades = load_open_trades()
    prices = load_live_prices()

    for trade in trades:
        symbol = trade['symbol']
        current_price = prices.get(symbol)
        if current_price is None:
            continue

        direction = 1 if trade['action'] == 'BUY' else -1
        atr = 1.0  # Temporary fixed ATR until dynamic ATR is wired in
        tp1 = trade['entry_price'] + direction * (atr * trade['tp1_mult'])
        tp2 = trade['entry_price'] + direction * (atr * trade['tp2_mult'])
        tp3 = trade['entry_price'] + direction * (atr * trade['tp3_mult'])

        # Handle trailing stop logic
        if trade['trailing_tp_active']:
            # Expiration timeout check
            if trade['tp_timeout_start']:
                elapsed = datetime.now() - trade['tp_timeout_start']
                if elapsed.total_seconds() > trade['trail_timeout']:
                    print(f"⏰ Timeout hit for {symbol}, closing at market.")
                    trade['contracts_remaining'] = 0
                    continue

            if trade['tp_trail_price'] is None:
                trade['tp_trail_price'] = current_price - direction * trade['trail_offset']
                trade['tp_timeout_start'] = datetime.now()
            else:
                new_trail = current_price - direction * trade['trail_offset']
                if direction * new_trail > direction * trade['tp_trail_price']:
                    trade['tp_trail_price'] = new_trail
                    trade['tp_timeout_start'] = datetime.now()

                if direction * current_price <= direction * trade['tp_trail_price']:
                    print(f"📉 Trailing stop hit for {symbol} at {current_price}")
                    trade['contracts_remaining'] = 0
                    continue

        # Handle stepwise take-profits
        if trade['tp_hit_stage'] == 0 and direction * current_price >= direction * tp1:
            print(f"✅ TP1 hit for {symbol}")
            trade['contracts_remaining'] -= 1
            trade['tp_hit_stage'] = 1
            trade['sl_price'] = trade['entry_price'] + direction * trade['be_offset']
            trade['trailing_tp_active'] = True
            trade['tp_timeout_start'] = datetime.now()
        elif trade['tp_hit_stage'] == 1 and direction * current_price >= direction * tp2:
            print(f"✅ TP2 hit for {symbol}")
            trade['contracts_remaining'] -= 1
            trade['tp_hit_stage'] = 2
        elif trade['tp_hit_stage'] == 2 and direction * current_price >= direction * tp3:
            print(f"✅ TP3 hit for {symbol}")
            trade['contracts_remaining'] -= 1
            trade['tp_hit_stage'] = 3

        # Handle hard stop-loss
        if direction * current_price <= direction * trade['sl_price']:
            print(f"🛑 SL hit for {symbol} at {current_price}")
            trade['contracts_remaining'] = 0

    # TODO: Write back updated trades to CSV or handle execution

if __name__ == "__main__":
    while True:
        monitor_trades()
        time.sleep(10)