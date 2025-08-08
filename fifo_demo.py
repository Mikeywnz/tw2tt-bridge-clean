import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone

# Initialize Firebase app (adjust the path and URL as needed)
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase_key.json")  # Your Firebase key path here
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app'
    })

firebase_db = db  # Make firebase_db global to be used in your FIFO function

# ========================= Your FIFO function ===========================
def fifo_match_and_flatten(active_trades, symbol):
    print(f"[DEBUG] fifo_match_and_flatten() called with {len(active_trades)} active trades")
    # ===== Archive and Delete Matched Trades =====
    matched_trades = [t for t in active_trades if t.get('exited') or t.get('trade_state') == 'closed']
    print(f"[DEBUG] Found {len(matched_trades)} matched trades to archive and delete")
    # For demo, skipping archive_and_delete call, just print instead
    for t in matched_trades:
        print(f"[INFO] Would archive and delete trade {t.get('trade_id')}")
    print(f"[DEBUG] Completed archiving and deleting matched trades")

    # Remove matched trades from active_trades list to avoid reprocessing
    active_trades = [t for t in active_trades if t not in matched_trades]
    print(f"[DEBUG] {len(active_trades)} trades remain active after cleanup")

    exit_trades = [t for t in active_trades if t.get('exit_in_progress') and not t.get('exited')]
    open_trades = [t for t in active_trades if not t.get('exited') and not t.get('exit_in_progress')]

    print(f"[DEBUG] Found {len(exit_trades)} exit trades and {len(open_trades)} open trades for matching")

    for exit_trade in exit_trades:
        matched = False
        for open_trade in open_trades:
            if open_trade.get('action') != exit_trade.get('exit_action') and not open_trade.get('exited'):
                open_trade['exited'] = True
                open_trade['trade_state'] = 'closed'
                open_trade['contracts_remaining'] = 0

                # Update Firebase to reflect trade exit
                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{open_trade['symbol']}")
                    open_trades_ref.child(open_trade['trade_id']).update({
                        "exited": True,
                        "trade_state": "closed",
                        "contracts_remaining": 0
                    })
                    print(f"[INFO] FIFO matched exit trade {exit_trade.get('trade_id')} to open trade {open_trade.get('trade_id')} and updated Firebase")
                except Exception as e:
                    print(f"❌ Failed to update Firebase for trade {open_trade.get('trade_id')}: {e}")

                matched = True
                break
        if not matched:
            print(f"[WARN] No matching open trade found for exit trade {exit_trade.get('trade_id')}")

    return active_trades


# ========================= Demo data ===========================
active_trades_sample = [
    {
        'trade_id': '1001',
        'symbol': 'MGC2510',
        'action': 'BUY',
        'exit_action': 'SELL',
        'exited': False,
        'trade_state': 'open',
        'contracts_remaining': 1,
        'exit_in_progress': False
    },
    {
        'trade_id': '2001',
        'symbol': 'MGC2510',
        'action': 'SELL',
        'exit_action': '',
        'exited': False,
        'trade_state': 'open',
        'contracts_remaining': 1,
        'exit_in_progress': True
    }
]

# ========================= Run Demo ===========================
print("Starting FIFO matching and archiving demo...\n")

print("Before FIFO match:")
for t in active_trades_sample:
    print(t)

updated_trades = fifo_match_and_flatten(active_trades_sample, 'MGC2510')

print("\nAfter FIFO match:")
for t in updated_trades:
    print(t)

print("\nFIFO matching and archiving demo complete.")