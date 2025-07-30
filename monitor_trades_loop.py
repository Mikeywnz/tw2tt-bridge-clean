# ========================= MONITOR_TRADES_LOOP - 1 & 2 COMBINED ================================
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import time
from datetime import datetime, timezone
from pytz import timezone
import requests
import subprocess
import firebase_active_contract
import os

# Trade fields usage:
# - trade_type: LONG_ENTRY, SHORT_ENTRY, FLATTENING_BUY, FLATTENING_SELL, etc. (Classification of trade)
# - status: FILLED, CANCELLED, EXPIRED, CLOSED, etc. (Order execution status)
# - trade_state: "open" or "closed" (Used for filtering trades in Firebase)
#
# Important: Do NOT set trade_type to "closed". Use 'status' or 'trade_state' to indicate closure.

# Load Firebase secret key
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# === FIREBASE INITIALIZATION ===
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
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
        print(f"📤 Exit order sent: {exit_action} 1 {symbol}")
        print("stdout:", result.stdout.strip())
        print("stderr:", result.stderr.strip())
    except Exception as e:
        print(f"❌ Failed to execute exit order: {e}")


# === Helper: Check if trade is archived ===
def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

# 🟢 Archive trade HELPER to /archived_trades/ before deletion
def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{trade_id}")
        if "trade_type" not in trade or not trade["trade_type"]:
            trade["trade_type"] = "UNKNOWN"
        archive_ref.set(trade)
        print(f"[DEBUG] Archiving trade {trade_id} with trade_type: {trade.get('trade_type')}")
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

# ==========================
# 🟩 FIFO MATCHING PATCH START
# ==========================

def fifo_match_and_flatten(active_trades):
    """
    Perform FIFO flattening ONLY on trades labeled as FLATTENING_BUY or FLATTENING_SELL,
    adjusting contracts_remaining and marking trades exited when fully flattened.
    """
    buys = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_BUY' and t.get('contracts_remaining', 0) > 0]
    sells = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_SELL' and t.get('contracts_remaining', 0) > 0]

    buys.sort(key=lambda x: x.get('entry_timestamp', x['trade_id']))
    sells.sort(key=lambda x: x.get('entry_timestamp', x['trade_id']))

    print(f"[DEBUG] FIFO Matching {len(buys)} buys and {len(sells)} sells")

    i, j = 0, 0
    while i < len(buys) and j < len(sells):
        buy = buys[i]
        sell = sells[j]

        print(f"[DEBUG] Considering BUY {buy['trade_id']} and SELL {sell['trade_id']}")

        buy_remain = buy.get('contracts_remaining', 0)
        sell_remain = sell.get('contracts_remaining', 0)

        print(f"    Before flatten: BUY contracts_remaining={buy_remain}, SELL contracts_remaining={sell_remain}")

        if buy_remain <= 0:
            print(f"    BUY {buy['trade_id']} depleted, moving to next buy.")
            i += 1
            continue
        if sell_remain <= 0:
            print(f"    SELL {sell['trade_id']} depleted, moving to next sell.")
            j += 1
            continue

        qty = min(buy_remain, sell_remain)

        buy['contracts_remaining'] -= qty
        sell['contracts_remaining'] -= qty

        print(f"🟢 FIFO flattening {qty} contracts: BUY {buy['trade_id']} with SELL {sell['trade_id']}")

        for trade in (buy, sell):
            if trade['contracts_remaining'] == 0 and not trade.get('exited'):
                trade['exited'] = True
                trade['status'] = 'closed'
                trade['trade_state'] = 'closed'
                print(f"✅ Trade {trade['trade_id']} fully flattened and closed via FIFO match")

        print(f"    After flatten: BUY contracts_remaining={buy['contracts_remaining']}, SELL contracts_remaining={sell['contracts_remaining']}")

        if buy['contracts_remaining'] == 0:
            i += 1
        if sell['contracts_remaining'] == 0:
            j += 1

# ==========================
# 🟩 FIFO MATCHING PATCH END
# ==========================

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

# ==========================
# 🟩 GREEN PATCH START: Zombie & Ghost Trade Handler with 30s Grace Period
# ==========================

ZOMBIE_COOLDOWN_SECONDS = 30
GHOST_GRACE_PERIOD_SECONDS = 30
ZOMBIE_STATUSES = {"FILLED"}  # Legitimate filled trades with no position
GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}

def handle_zombie_and_ghost_trades(firebase_db):
    now_utc = datetime.now(timezone("UTC"))
    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")
    ghost_trades_ref = firebase_db.reference("/ghost_trades_log")

    all_open_trades = open_trades_ref.get() or {}
    existing_zombies = set(zombie_trades_ref.get() or {})
    existing_ghosts = set(ghost_trades_ref.get() or {})

    now_nz = datetime.now(timezone("Pacific/Auckland"))

    try:
        position_count = int(firebase_db.reference("/live_total_positions/position_count").get())
    except Exception:
        position_count = 0

    if position_count > 0:
        print("[INFO] Positions open; skipping zombie and ghost cleanup.")
        return

    if not isinstance(all_open_trades, dict):
        print("⚠️ all_open_trades is not a dict, skipping trade processing")
        return

    for symbol, trades_by_id in all_open_trades.items():
        if not isinstance(trades_by_id, dict):
            print(f"⚠️ Skipping trades for symbol {symbol} because it's not a dict")
            continue

        for trade_id, trade in trades_by_id.items():
            if not isinstance(trade, dict):
                continue

            status = trade.get("status", "").upper()
            filled = trade.get("filled", 0)

            if trade_id in existing_zombies or trade_id in existing_ghosts:
                continue

            if status in GHOST_STATUSES and filled == 0:
                print(f"👻 Archiving ghost trade {trade_id} for symbol {symbol} (no timestamp needed)")
                ghost_trades_ref.child(trade_id).set(trade)
                open_trades_ref.child(symbol).child(trade_id).delete()
                print(f"🗑️ Deleted ghost trade {trade_id} from /open_active_trades/")
                continue

            entry_ts_str = trade.get("entry_timestamp")
            if not entry_ts_str:
                print(f"⚠️ No entry_timestamp for trade {trade_id}; skipping cooldown check")
                continue

            try:
                entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00")).astimezone(timezone("UTC"))
            except Exception as e:
                print(f"⚠️ Failed to parse entry_timestamp for {trade_id}: {e}; skipping cooldown check")
                continue

            age_seconds = (now_utc - entry_ts).total_seconds()

            if status in ZOMBIE_STATUSES:
                if age_seconds < ZOMBIE_COOLDOWN_SECONDS:
                    print(f"⏳ Zombie trade {trade_id} age {age_seconds:.1f}s < cooldown {ZOMBIE_COOLDOWN_SECONDS}s — skipping")
                    continue
                print(f"🧟‍♂️ Archiving zombie trade {trade_id} for symbol {symbol} (age {age_seconds:.1f}s)")
                zombie_trades_ref.child(trade_id).set({
                    "symbol": symbol,
                    "trade_data": trade
                })
                open_trades_ref.child(symbol).child(trade_id).delete()
                print(f"🗑️ Deleted zombie trade {trade_id} from /open_active_trades()")

# ==========================
# 🟩 GREEN PATCH END
# ==========================

def monitor_trades():
    active_trades = []
    print(f"[DEBUG] Starting zombie check in monitor_trades at {datetime.now(timezone('Pacific/Auckland'))}")

    if not check_live_positions_freshness(db, grace_period_seconds=GRACE_PERIOD_SECONDS):
        print("[DEBUG] Skipping zombie trade check due to stale data or non-zero positions")
    else:
        print("[DEBUG] Passing zombie trade check, handling zombies")
        handle_zombie_and_ghost_trades(db)

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

    # Consolidated active_trades building
    for t in all_trades:
        tid = t.get('trade_id')

        if not tid:
            print("⚠️ Skipping trade with no trade_id")
            continue

        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {tid}")
            continue

        GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}

        if not t.get('filled') and t.get('status', '').upper() not in GHOST_STATUSES:
            print(f"🧾 Skipping {tid} ⚠️ not filled and not a ghost trade")
            continue

        status = t.get('status', '').upper()
        if t.get('contracts_remaining', 0) <= 0 and status not in GHOST_STATUSES:
            print(f"🧾 Skipping {tid} ⚠️ no contracts remaining and not a ghost trade")
            continue

        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

    # === FIFO matching with detailed debug ===
    def log_trade(trade):
        print(f"    Trade {trade['trade_id']}: contracts_remaining={trade.get('contracts_remaining', 0)}, exited={trade.get('exited', False)}")

    buys = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_BUY' and t.get('contracts_remaining', 0) > 0]
    sells = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_SELL' and t.get('contracts_remaining', 0) > 0]

    buys.sort(key=lambda x: x.get('entry_timestamp', x['trade_id']))
    sells.sort(key=lambda x: x.get('entry_timestamp', x['trade_id']))

    print(f"[DEBUG] FIFO Matching {len(buys)} buys and {len(sells)} sells")

    i, j = 0, 0
    while i < len(buys) and j < len(sells):
        buy = buys[i]
        sell = sells[j]

        print(f"[DEBUG] Considering BUY {buy['trade_id']} and SELL {sell['trade_id']}")

        buy_remain = buy.get('contracts_remaining', 0)
        sell_remain = sell.get('contracts_remaining', 0)

        log_trade(buy)
        log_trade(sell)

        if buy_remain <= 0:
            print(f"    BUY {buy['trade_id']} contracts depleted, moving to next buy.")
            i += 1
            continue
        if sell_remain <= 0:
            print(f"    SELL {sell['trade_id']} contracts depleted, moving to next sell.")
            j += 1
            continue

        qty = min(buy_remain, sell_remain)

        buy['contracts_remaining'] -= qty
        sell['contracts_remaining'] -= qty

        print(f"🟢 FIFO flattening {qty} contracts: BUY {buy['trade_id']} with SELL {sell['trade_id']}")

        for trade in (buy, sell):
            if trade['contracts_remaining'] == 0 and not trade.get('exited'):
                trade['exited'] = True
                trade['status'] = 'closed'
                trade['trade_state'] = 'closed'
                print(f"✅ Trade {trade['trade_id']} fully flattened and closed via FIFO match")

        if buy['contracts_remaining'] == 0:
            i += 1
        if sell['contracts_remaining'] == 0:
            j += 1

        # ===== END OF PART 1 & 2 =====

#=========================  MONITOR_TRADES_LOOP - PART 3 (FINAL PART)  ================================

    updated_trades = []
    prices = load_live_prices()

    fifo_match_and_flatten(active_trades)

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

       # if trade.get("is_ghost", False):
        #    print(f"⏭️ Skipping already exited/in-progress trade {trade_id}")
         #   continue

        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {trade_id}")
            updated_trades.append(trade)
            continue

        entry = trade.get('filled_price')
        if entry is None:
            print(f"❌ Trade {trade.get('trade_id', 'unknown')} missing filled_price, skipping.")
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
                            trade['trade_state'] = "closed"
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
        if not t.get('exited')
        and t.get('status') != 'closed'
        and t.get('trade_state') != 'closed'
        and t.get('contracts_remaining', 0) > 0
        and t.get('filled')
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
