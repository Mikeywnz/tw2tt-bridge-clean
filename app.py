from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
import os

app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        # Content type check
        if request.headers.get("content-type") != "application/json":
            return JSONResponse(status_code=415, content={"error": "Unsupported Media Type"})

        # Parse JSON payload
        data = await request.json()
        alert_type = data.get("type", "").lower()
        symbol = data.get("symbol", "").strip()
        price = float(data.get("price", 0))
        qty = int(data.get("qty", 1))
        action = data.get("action", "").upper()

        # === 1. Price Update Logic (preserve multiple symbols) ===
        if alert_type == "price_update":
            price_data = {}
            if os.path.exists("live_prices.json"):
                with open("live_prices.json", "r") as f:
                    try:
                        price_data = json.load(f)
                    except json.JSONDecodeError:
                        price_data = {}

            price_data[symbol] = price  # update or insert
            with open("live_prices.json", "w") as f:
                json.dump(price_data, f)

            print(f"✅ Price updated: {symbol} = {price}")
            return {"status": "price updated"}

        # === 2. Trade Execution Logic ===
        elif alert_type in ["buy", "sell"]:
            from execute_trade_live import place_order
            place_order(symbol=symbol, action=alert_type, quantity=qty)
            return {"status": f"trade executed: {alert_type} {qty} of {symbol}"}

        else:
            print("⚠️ Unknown alert type:", alert_type)
            return JSONResponse(status_code=400, content={"error": "Unknown alert type"})

    except Exception as e:
        print("❌ Webhook error:", e)
        return JSONResponse(status_code=500, content={"error": str(e)})