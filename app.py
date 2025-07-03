import os
import json
from fastapi import FastAPI, Request

app = FastAPI()

# === Use absolute path to ensure compatibility with Render's filesystem ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
live_prices_path = os.path.join(BASE_DIR, "live_prices.json")

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    if data.get("type") == "price_update":
        symbol = data.get("symbol")
        price = data.get("price")

        # === Step 1: Load existing prices ===
        if os.path.exists(live_prices_path):
            with open(live_prices_path, "r") as f:
                prices = json.load(f)
        else:
            prices = {}

        # === Step 2: Update and save new price ===
        prices[symbol] = price
        with open(live_prices_path, "w") as f:
            json.dump(prices, f)

        print(f"💾 Updated price: {symbol} = {price}")
        return {"status": "price updated"}

    return {"status": "ignored"}