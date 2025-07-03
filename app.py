import os
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# === Path Setup ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_PRICES_FILE = os.path.join(BASE_DIR, "live_prices.json")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        print("🚀 Incoming webhook:", data)

        # === PRICE UPDATE ===
        if data.get("type") == "price_update":
            symbol = data.get("symbol")
            price = data.get("price")

            # Load existing live prices if available
            if os.path.exists(LIVE_PRICES_FILE):
                with open(LIVE_PRICES_FILE, "r") as f:
                    prices = json.load(f)
            else:
                prices = {}

            # Update the price
            prices[symbol] = price
            with open(LIVE_PRICES_FILE, "w") as f:
                json.dump(prices, f)

            print(f"💾 Updated {symbol} price to {price}")
            return {"status": "price updated"}

        # === TRADE ALERT (buy/sell) ===
        elif data.get("type") in ["BUY", "SELL"]:
            # Add your trade handling logic here if needed
            print(f"📈 Received trade alert: {data}")
            return {"status": "trade alert received"}

        else:
            return JSONResponse(status_code=400, content={"error": "Unknown webhook type"})

    except Exception as e:
        print("❌ Error:", e)
        return JSONResponse(status_code=500, content={"error": "Webhook failed"})