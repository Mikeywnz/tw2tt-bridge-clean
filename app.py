from fastapi import FastAPI, Request
import json
import os

app = FastAPI()

# Determine absolute path to the JSON file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_PRICES_FILE = os.path.join(BASE_DIR, "live_prices.json")

@app.post("/webhook")
async def webhook_listener(request: Request):
    data = await request.json()
    symbol = data.get("symbol")
    price = data.get("price")
    update_type = data.get("type")

    if update_type == "price_update" and symbol and price:
        # Load current prices
        if os.path.exists(LIVE_PRICES_FILE):
            with open(LIVE_PRICES_FILE, "r") as f:
                prices = json.load(f)
        else:
            prices = {}

        # Update the symbol's price
        prices[symbol] = price

        # Save updated prices back
        with open(LIVE_PRICES_FILE, "w") as f:
            json.dump(prices, f)

        return {"status": "price updated"}

    return {"status": "ignored"}