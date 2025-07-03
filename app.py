from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json

app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        # Try to parse JSON regardless of content-type
        try:
            data = await request.json()
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        alert_type = data.get("type", "").lower()
        symbol = data.get("symbol", "").strip()
        price = float(data.get("price", 0))
        qty = int(data.get("qty", 1))
        action = data.get("action", "").upper()

        if alert_type == "price_update":
            # ✅ FIXED: Store multiple symbols now
            try:
                with open("live_prices.json", "r") as f:
                    all_prices = json.load(f)
            except:
                all_prices = {}

            all_prices[symbol] = price
            with open("live_prices.json", "w") as f:
                json.dump(all_prices, f)

            print(f"✅ Price updated: {symbol} = {price}")
            return {"status": "price updated"}

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