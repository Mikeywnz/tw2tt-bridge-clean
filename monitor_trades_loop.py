#=========================  MONITOR_TRADES_LOOP - PART 1  ================================
import firebase_admin
from firebase_admin import credentials, db
import time
from datetime import datetime
from pytz import timezone
import requests 
import subprocess
import firebase_active_contract
import firebase_admin
from firebase_admin import credentials, initialize_app, db

# Load Firebase secret key
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# === FIREBASE INITIALIZATION ===
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

# === Load live prices from Firebase ===
def load_live_prices():
    return db.reference("live_prices").get() or {}

# === Helper to execute exit trades ===
def close_position(symbol, original_action):
    exit_action = "SELL" if original_action == "BUY" else "BUY"
    try:
        result = subprocess.run(
            ["python3", "execute_trade_live.py", symbol, exit_action, "1"],
            capture_output=True,
            text=True
        )
        print(f"\U0001f4e4 Exit order sent: {exit_action} 1 {symbol}")
        print("stdout:", result.stdout.strip())
        print("stderr:", result.stderr.strip())
    except Exception as e:
        print(f"❌ Failed to execute exit order: {e}")

# === Helper: Check if trade is archived ===
def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/tiger_orders_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

# 🟢 Archive trade HELPER to /archived_trades/ before deletion
def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades/{trade_id}")
        trade["trade_type"] = "closed"
        archive_ref.set(trade)
        print(f"✅ Archived trade {trade_id}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {trade_id}: {e}")
        return False

# === Firebase open trades handlers ===
def load_open_trades(symbol):
    firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_active_trades/{symbol}.json"
    try:
        resp = requests.get(firebase_url)
        resp.raise_for_status()
        data = resp.json() or {}
        trades = []
        if isinstance(data, dict):
            for tid, td in data.items():
                td['trade_id'] = tid
                trades.append(td)
        else:
            trades = []

        print(f"🔄 Loaded {len(trades)} open trades from Firebase.")
        return trades
    except Exception as e:
        print(f"❌ Failed to fetch open trades: {e}")
        return []

def save_open_trades(symbol, trades):
    try:
        for t in trades:
            trade_id = t.get("trade_id")
            if not trade_id:
                continue
            firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_active_trades/{symbol}/{trade_id}.json"
            requests.put(firebase_url, json=t).raise_for_status()
            print(f"✅ Saved trade {trade_id} to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

def delete_trade_from_firebase(symbol, trade_id):
    firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_active_trades/{symbol}/{trade_id}.json"
    try:
        resp = requests.delete(firebase_url)
        resp.raise_for_status()
        print(f"✅ Deleted trade {trade_id} from Firebase.")
        return True
    except Exception as e:
        print(f"❌ Failed to delete trade {trade_id} from Firebase: {e}")
        return False

def load_trailing_tp_settings():
    try:
        fb_url = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/trailing_tp_settings.json"
        res = requests.get(fb_url)
        cfg = res.json() if res.ok else {}
        if cfg.get("enabled", False):
            trigger = float(cfg.get("trigger_points", 14.0))
            offset = float(cfg.get("offset_points", 5.0))
            print(f"📐 Loaded trailing TP config: trigger={trigger}, offset={offset}")
            return trigger, offset
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0

# ===== END OF PART 1 =====

#=========================  MONITOR_TRADES_LOOP - PART 2  ================================

# === MONITOR LOOP ===
exit_in_progress = set()
GRACE_PERIOD_SECONDS = 30

# === Step 2a: Check Live Positions Freshness ===
def check_live_positions_freshness(firebase_db, grace_period_seconds=140):

        live_ref = firebase_db.reference("/live_total_positions")
        data = live_ref.get() or {}

        position_count = data.get("position_count", None)
        last_updated_str = data.get("last_updated", None)

        if position_count is None or last_updated_str is None:
            print("⚠️ /live_total_positions data incomplete or missing")
            return False

        try:
            nz_tz = timezone("Pacific/Auckland")
            last_updated = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M:%S NZST")
            last_updated = nz_tz.localize(last_updated)
        except Exception as e:
            print(f"⚠️ Failed to parse last_updated timestamp: {e}")
            return False

        now_nz = datetime.now(nz_tz)
        delta_seconds = (now_nz - last_updated).total_seconds()

        if delta_seconds > grace_period_seconds:
            print(f"⚠️ Firebase data too old ({delta_seconds:.1f}s), skipping zombie check")
            return False

        print(f"✅ Firebase data fresh ({delta_seconds:.1f}s ago), position_count = {position_count}")

        if position_count == 0 or position_count == 0.0:
            return True
        else:
            return False

# === Step 2b: Handle Zombie Trades ===
def handle_zombie_trades(firebase_db):
    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")

    all_open_trades = open_trades_ref.get() or {}
    existing_zombies = set(zombie_trades_ref.get() or {})

    if isinstance(all_open_trades, dict):
        for symbol, trades_by_id in all_open_trades.items():
            if isinstance(trades_by_id, dict):
                for trade_id, trade in trades_by_id.items():
                    if not isinstance(trade, dict):
                        continue

                    if trade_id in existing_zombies:
                        print(f"⏭️ Skipping already known zombie trade: {trade_id}")
                        continue

                    # Mark as zombie
                    print(f"🛑 Marking zombie trade: {trade_id} for symbol {symbol}")

                    # Add to zombie_trades_log list
                    zombie_trades_ref.child(trade_id).set({
                        "symbol": symbol,
                        "trade_data": trade
                    })

                    # Delete trade from open_active_trades to prevent re-entry
                    open_trades_ref.child(symbol).child(trade_id).delete()
                    print(f"🗑️ Deleted zombie trade {trade_id} from /open_active_trades/")
            else:
                print(f"⚠️ Skipping trades_by_id for symbol {symbol} because it's not a dict")
    else:
        print("⚠️ all_open_trades is not a dict, skipping trade processing")

def monitor_trades():
    if not check_live_positions_freshness(db, grace_period_seconds=GRACE_PERIOD_SECONDS):
        print("⚠️ Skipping zombie trade check — live positions data not fresh or non-zero")
    else:
        handle_zombie_trades(db)

    trigger_points, offset_points = load_trailing_tp_settings()
    current_time = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    if current_time - monitor_trades.last_heartbeat >= 60:
        active_symbol = firebase_active_contract.get_active_contract()
    if not active_symbol:
        print("❌ No active contract symbol for live price fetch")
        mgc_price = None
    else:
        mgc_price = load_live_prices().get(active_symbol, {}).get('price')

    print(f"🛰️ System working – {active_symbol} price: {mgc_price}")
        monitor_trades.last_heartbeat = current_time

    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found in Firebase; aborting monitor_trades")
        return
    all_trades = load_open_trades(symbol)

    for t in all_trades:
        tid = t.get('trade_id')

        if not tid:
            print("⚠️ Skipping trade with no trade_id")
            continue

        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {tid}")
            continue

        if not t.get('filled'):
            print(f"🧾 Skipping {tid} ⚠️ not filled")
            continue

        if t.get('contracts_remaining', 0) <= 0:
            print(f"🧾 Skipping {tid} ⚠️ no contracts remaining")
            continue

    active_trades = []
    for t in all_trades:
        if not t or not isinstance(t, dict):
            continue

        tid = t.get('trade_id')
        if not tid:
            print("⚠️ Skipping trade with no trade_id")
            continue

        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"⏭️ Skipping exited/closed trade {t.get('trade_id', 'unknown')}")
            continue

        if not t.get('filled') or t.get('contracts_remaining', 0) <= 0:
            continue

        if t.get("is_ghost", False):
            print(f"⏭️ Skipping ghost trade {tid}")
            continue

        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

        # ===== END OF PART 2 =====

#=========================  MONITOR_TRADES_LOOP - PART 3 (FINAL PART)  ================================

    updated_trades = []
    prices = load_live_prices()

    for trade in active_trades:
        if not trade or not isinstance(trade, dict):
            continue
        if trade.get("status") == "closed":
            print(f"🔒 Skipping closed trade {trade.get('trade_id')}")
            continue
        trade_id = trade.get('trade_id', 'unknown')
        print(f"🔄 Processing trade {trade_id}")
        if trade.get('exited') or trade_id in exit_in_progress:
            continue
        if trade.get("is_ghost", False):
            print(f"⏭️ Skipping already exited/in-progress trade {trade_id}")
            continue

        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {trade_id}")
            updated_trades.append(trade)
            continue

        entry = trade['entry_price']
        if entry <= 0:
            print(f"❌ Invalid entry price for {trade_id} — skipping.")
            continue

        # 🟢 Trailing TP Exit Handling with Archive & Delete
        tp_trigger = trigger_points

        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            if (direction == 1 and current_price >= entry + tp_trigger) or (direction == -1 and current_price <= entry - tp_trigger):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"🎯 TP trigger hit for {trade_id} → trail activated at {current_price}")

        if trade.get('trail_hit'):
            if (direction == 1 and current_price > trade['trail_peak']) or (direction == -1 and current_price < trade['trail_peak']):
                trade['trail_peak'] = current_price
            buffer_amt = offset_points
            if (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt):
                print(f"🚨 Trailing TP exit for {trade_id}: price={current_price}, peak={trade['trail_peak']}")
                exit_in_progress.add(trade_id)
                close_position(symbol, trade['action'])

                # Archive then delete trade
                try:
                    archived = archive_trade(symbol, trade)
                    if archived:
                        success = delete_trade_from_firebase(symbol, trade_id)
                        if success:
                            print(f"✅ Trade {trade_id} archived and deleted from open trades.")
                            trade['exited'] = True
                            trade['status'] = "closed"
                        else:
                            print(f"❌ Trade {trade_id} deletion failed after archiving.")
                    else:
                        print(f"❌ Trade {trade_id} archiving failed, skipping deletion.")
                except Exception as e:
                    import traceback
                    print(f"❌ Exception during archive/delete for trade {trade_id}: {e}")
                    traceback.print_exc()
                continue

        print(f"📌 Keeping trade {trade.get('trade_id')} OPEN – trail_hit={trade.get('trail_hit')}, exited={trade.get('exited')}, status={trade.get('status')}")
        updated_trades.append(trade)


    # ✅ Only save valid open trades back to Firebase
    filtered_trades = [
        t for t in updated_trades
        if not t.get('exited') and
        t.get('status') != 'closed' and t.get('trade_state') != 'closed' and
        t.get('contracts_remaining', 0) > 0 and
        t.get('filled') and
        not t.get('is_ghost', False)
    ]
    save_open_trades(symbol, filtered_trades)

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

#=====  END OF PART 3 (END OF SCRIPT)  =====

