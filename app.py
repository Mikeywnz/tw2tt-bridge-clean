from flask import Flask, request, jsonify

app = Flask(__name__)

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