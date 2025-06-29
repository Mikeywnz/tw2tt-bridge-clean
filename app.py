from flask import Flask, request, jsonify
import subprocess

app = Flask(__name__)

def run_live_trade(symbol, action, quantity):
    try:
        result = subprocess.run(
            ["python3", "execute_trade_live.py", symbol, action, str(quantity)],
            capture_output=True,
            text=True
        )
        print(f"📦 TigerTrade Execution Return Code: {result.returncode}")
        print("📬 TigerTrade Execution Output:")
        print(result.stdout)
        print("⚠️ TigerTrade Execution Errors:")
        print(result.stderr)
    except Exception as e:
        print("❌ Error launching TigerTrade process:", e)

@app.route('/webhook', methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 Webhook received:", data)

        symbol = data.get("symbol")
        action = data.get("action")
        quantity = data.get("quantity")

        if symbol and action and quantity:
            print(f"✅ Parsed: {symbol} | {action} | {quantity}")
            run_live_trade(symbol, action, quantity)
            return jsonify({"success": True})
        else:
            print("⚠️ Incomplete data received.")
            return jsonify({"success": False, "error": "Missing fields"}), 400

    except Exception as e:
        print("❌ Error in webhook handler:", e)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/')
def hello():
    return "✅ All good."