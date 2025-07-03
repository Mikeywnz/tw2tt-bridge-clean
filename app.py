from fastapi import FastAPI, Request
import json
import os

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
live_prices_path = os.path.join(BASE_DIR, "live_prices.json")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body_bytes = await request.body()
        print("📩 Raw body received:", body_bytes)

        if not body_bytes:
            return {"error": "Empty request body"}

        try:
            data = json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            return {"error": "Malformed JSON"}

        if data.get("type") == "price_update":
            symbol = data.get("symbol")
            price = data.get("price")

            if symbol and price:
                if os.path.exists(live_prices_path):
                    with open(live_prices_path, "r") as f:
                        prices = json.load(f)
                else:
                    prices = {}

                prices[symbol] = price

                with open(live_prices_path, "w") as f:
                    json.dump(prices, f)

                return {"status": "price updated"}

            return {"error": "Missing symbol or price"}

        return {"status": "ignored"}

    except Exception as e:
        return {"error": str(e)}

   if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=10000)