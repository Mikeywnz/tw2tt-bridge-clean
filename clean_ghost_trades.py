import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone, timedelta
import os

# Initialize Firebase app if not already
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

def delete_old_trades(log_path, age_hours=12):
    ref = db.reference(log_path)
    trades = ref.get() or {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=age_hours)

    for trade_id, trade_data in trades.items():
        timestamp_str = trade_data.get('timestamp') or trade_data.get('time') or trade_data.get('transacted_at')
        if not timestamp_str:
            print(f"Skipping trade {trade_id}: no timestamp found in {log_path}")
            continue
        try:
            trade_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if trade_time < cutoff:
                ref.child(trade_id).delete()
                print(f"Deleted trade {trade_id} from {log_path} older than {age_hours} hours")
        except Exception as e:
            print(f"Error parsing timestamp for trade {trade_id} in {log_path}: {e}")

if __name__ == "__main__":
    delete_old_trades("/ghost_trades_log")
    delete_old_trades("/zombie_trades_log")
    delete_old_trades("/archived_trades_log")