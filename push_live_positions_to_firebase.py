#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  ================================
import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
from firebase_admin import credentials, initialize_app, db
from datetime import datetime
from pytz import timezone
import os

# === Firebase Initialization ===
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

initialize_app(cred, {
    'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
})

# === TigerOpen Client Setup ===
config = TigerOpenClientConfig()
client = TradeClient(config)


# === Helper: Fetch Trade Transactions from Tiger (for accurate entry prices) ===
def fetch_trade_transactions(account_id):
    try:
        transactions = client.get_transactions(account=account_id, symbol="MGC2508", limit=20)
        return transactions or []
    except Exception as e:
        print(f"❌ Failed to fetch trade transactions: {e}")
        return []


# === Main Loop: Push Live Positions and Sync Entry Prices to Firebase ===
def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    open_trades_ref = db.reference("/open_active_trades")

    while True:
        try:
            # --- Update position count ---
            positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
            position_count = sum(getattr(pos, "quantity", 0) for pos in positions)
            timestamp_iso = datetime.utcnow().isoformat() + 'Z'

            now_nz = datetime.now(timezone("Pacific/Auckland"))
            timestamp_readable = now_nz.strftime("%Y-%m-%d %H:%M:%S NZST")

            live_ref.update({
                "position_count": position_count,
                "last_updated": timestamp_readable
            })
            print(f"✅ Pushed position count {position_count} at {timestamp_iso}")

            # --- Fetch trade activities for accurate prices ---
            transactions = fetch_trade_transactions(account_id="21807597867063647")

            print(f"🔍 Fetched {len(transactions)} transactions")
            for tx in transactions:
                print(vars(tx))

            # --- Update Firebase open trades with accurate entry prices ---
            for tx in transactions:
                trade_key = getattr(tx, "order_id", None)  # Use order_id instead of transaction id
                symbol = None
                contract = getattr(tx, "contract", None)
                if contract:
                    symbol = str(contract).split('/')[0]
                entry_price = getattr(tx, "filled_price", None) or getattr(tx, "price", None)
                if not (trade_key and symbol and entry_price):
                    continue

                open_trade_ref = open_trades_ref.child(symbol).child(str(trade_key))
                existing_trade = open_trade_ref.get()
                print(f"Checking Firebase for trade key {trade_key} under symbol {symbol}")
                print(f"Existing trade data: {existing_trade}")
                if existing_trade:
                    existing_price = existing_trade.get("entry_price")
                    # Update only if entry_price missing or different
                    if existing_price != entry_price:
                        open_trade_ref.update({"entry_price": entry_price})
                        print(f"🔄 Updated entry price for trade {trade_key} on {symbol} to {entry_price}")

            # --- Keep /live_total_positions/ path alive ---
            if not live_ref.get():
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(5)  # Pause 5 seconds before next update


if __name__ == "__main__":
    push_live_positions()

#=========================  PUSH_LIVE_POSITIONS_TO_FIREBASE (END OF SCRIPT)  ================================