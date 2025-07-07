# src/utils.py
import csv
import json
import os

def append_to_open_trades(symbol, entry_price, action):
    csv_path = os.path.join(os.path.dirname(__file__), 'open_trades.csv')
    ema_path = os.path.join(os.path.dirname(__file__), 'ema_values.json')

    # Load EMA values
    try:
        with open(ema_path, 'r') as f:
            ema_data = json.load(f)
            ema9 = ema_data.get("ema9", "")
            ema20 = ema_data.get("ema20", "")
    except:
        ema9 = ""
        ema20 = ""

    # Append row
    with open(csv_path, 'a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            symbol,
            entry_price,
            action.upper(),
            1,          # contracts_remaining
            1.0,        # trail perc
            0.5,        # trail offset
            '',         # tp_trail_price
            ema9,
            ema20
        ])