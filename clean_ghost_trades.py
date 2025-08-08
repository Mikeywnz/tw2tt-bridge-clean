import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone, timedelta
import os

# Initialize Firebase app if not already initialized
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

def extract_trade_timestamp(trade_data):
    # Unwrap nested 'trade_data' if present
    if isinstance(trade_data, dict) and 'trade_data' in trade_data:
        trade_data = trade_data['trade_data']

    timestamp_fields = [
        'timestamp',
        'time',
        'transacted_at',
        'entry_timestamp',
        'exit_timestamp',
        'executed_timestamp'
    ]
    for field in timestamp_fields:
        val = trade_data.get(field)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception as e:
                print(f"❌ Failed parsing {field}='{val}': {e}")
    return None

def delete_old_trades(log_path, age_hours=12):
    ref = db.reference(log_path)
    trades = ref.get() or {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=age_hours)

    for trade_id, trade_data in trades.items():
        timestamp = extract_trade_timestamp(trade_data)
        if timestamp is None:
            print(f"Skipping trade {trade_id}: no valid timestamp found in {log_path}")
            continue

        if timestamp < cutoff:
            ref.child(trade_id).delete()
            print(f"Deleted trade {trade_id} from {log_path} older than {age_hours} hours")

if __name__ == "__main__":
    delete_old_trades("/ghost_trades_log")
    delete_old_trades("/zombie_trades_log")
    delete_old_trades("/archived_trades_log")