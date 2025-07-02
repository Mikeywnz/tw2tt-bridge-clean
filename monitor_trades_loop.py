import csv
import random
import time

def get_live_price(symbol):
    base = 100 if "MGC" in symbol else 5000 if "MES" in symbol else 80
    return round(base + random.uniform(-10, 10), 2)

while True:
    open_trades = []
    with open('open_trades.csv', 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            open_trades.append({
                "symbol": row['symbol'],
                "entry_price": float(row['entry_price']),
                "tp_price": float(row['tp_price']),
                "sl_price": float(row['sl_price']),
                "direction": row['action'].upper()
            })

    print("\n📊 Checking live prices...")
    for trade in open_trades:
        symbol = trade['symbol']
        price = get_live_price(symbol)
        direction = trade['direction']

        hit_tp = direction == "BUY" and price >= trade['tp_price']
        hit_sl = direction == "BUY" and price <= trade['sl_price']
        hit_tp = hit_tp or (direction == "SELL" and price <= trade['tp_price'])
        hit_sl = hit_sl or (direction == "SELL" and price >= trade['sl_price'])

        if hit_tp:
            print(f"\U0001F7E2 {symbol}: TP HIT! (Price: {price}) ✅")
        elif hit_sl:
            print(f"\U0001F534 {symbol}: SL HIT! (Price: {price}) ❌")
        else:
            print(f"\U0001F7E1 {symbol}: TP/SL not hit yet (Price: {price})")

    time.sleep(10)
    