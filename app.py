from fastapi import FastAPI, Request
import json
from datetime import datetime
import subprocess

app = FastAPI()

# === File paths ===
PRICE_FILE = "live_prices.json"
EMA_FILE = "ema_values.json"
TRADE_LOG = "trade_log.json"

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        print(f"❌ Failed to parse JSON: {e}")
        return {"status": "invalid json", "error": str(e)}

    print(f"📥 Incoming Webhook: {data}")

    # === Handle Price Update ===
    if data.get("type") == "price_update":
        symbol = data["symbol"]
        price = float(data["price"])

        # Load or initialize price store
        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}

        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)

        print(f"💾 Stored live price: {symbol} = {price}")
        return {"status": "price stored"}

    # === Handle EMA Update (multi-symbol) ===
    elif data.get("type") == "ema_update":
        symbol = data["symbol"]
        ema9 = float(data["ema9"])
        ema20 = float(data["ema20"])

        try:
            with open(EMA_FILE, "r") as f:
                ema_data = json.load(f)
        except FileNotFoundError:
            ema_data = {}

        ema_data[symbol] = {
            "ema9": ema9,
            "ema20": ema20,
            "updated_at": datetime.utcnow().isoformat()
        }

        with open(EMA_FILE, "w") as f:
            json.dump(ema_data, f, indent=2)

        print(f"💾 Stored EMAs for {symbol} — 9EMA={ema9}, 20EMA={ema20}")
        return {"status": "ema stored"}

    # === Handle Trade Signal ===
    elif data.get("action") in ("BUY", "SELL"):
        print(f"⚠️ Trade signal received: {data}")

        symbol = data["symbol"]
        action = data["action"]
        quantity = str(data.get("quantity", 1))

        try:
            print(f"🐅 Sending order to TigerTrade: {symbol} {action} x{quantity}")
            result = subprocess.run([
                "python3", "execute_trade_live.py",
                symbol,
                action,
                quantity
            ], capture_output=True, text=True)

            print("✅ TigerTrade stdout:", result.stdout)
            print("⚠️ TigerTrade stderr:", result.stderr)

        except Exception as e:
            print(f"❌ Failed to execute trade: {e}")

        return {"status": "trade signal received"}

    return {"status": "unhandled alert type"}