from fastapi import FastAPI, Request
import json
from datetime import datetime

app = FastAPI()

# File paths
PRICE_FILE = "live_prices.json"
EMA_FILE = "ema_values.json"
TRADE_LOG = "trade_log.json"

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"📥 Incoming Webhook: {data}")

    # === Handle Price Update ===
    if data.get("type") == "price_update":
        symbol = data["symbol"]
        price = float(data["price"])

        # Load current prices or initialize
        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}

        # Update and save
        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)

        print(f"💾 Stored live price: {symbol} = {price}")
        return {"status": "price stored"}

    # === Handle EMA Update ===
    elif data.get("type") == "ema_update":
        symbol = data["symbol"]
        ema9 = float(data["ema9"])
        ema20 = float(data["ema20"])

        ema_data = {
            "symbol": symbol,
            "ema9": ema9,
            "ema20": ema20,
            "updated_at": datetime.utcnow().isoformat()
        }

        with open(EMA_FILE, "w") as f:
            json.dump(ema_data, f, indent=2)

        print(f"💾 Stored EMA values for {symbol}: 9EMA={ema9}, 20EMA={ema20}")
        return {"status": "ema stored"}

    # === (Optional) Handle Trade Execution Alerts ===
    elif data.get("action") in ("BUY", "SELL"):
        print(f"⚠️ Trade signal received: {data}")
        # Add your trade logic or log it for now
        return {"status": "trade signal received"}

    return {"status": "unhandled alert type"}