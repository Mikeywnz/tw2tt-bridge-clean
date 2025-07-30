import firebase_admin
from firebase_admin import credentials, db
import os

# Initialize Firebase app if not already
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

def archive_and_delete_leftover_trades(symbol):
    open_ref = db.reference(f"/open_active_trades/{symbol}")
    archived_ref = db.reference(f"/archived_trades_log/{symbol}")

    open_trades = open_ref.get() or {}
    if not open_trades:
        print(f"No open trades found for {symbol}")
        return

    for trade_id, trade_data in open_trades.items():
        try:
            archived_ref.child(trade_id).set(trade_data)
            print(f"Archived trade {trade_id}")
            open_ref.child(trade_id).delete()
            print(f"Deleted trade {trade_id} from open_active_trades")
        except Exception as e:
            print(f"Error processing trade {trade_id}: {e}")

if __name__ == "__main__":
    symbol = "MGC2510"
    archive_and_delete_leftover_trades(symbol)