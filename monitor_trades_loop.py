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
    print(f"[DEBUG] close_position() called with original_action='{original_action}'")
    exit_action = "SELL" if original_action == "BUY" else "BUY"
    print(f"[DEBUG] close_position() using exit_action='{exit_action}'")
    try:
        result = subprocess.run(
            ["python3", "execute_trade_live.py", symbol, exit_action, "1"],
            capture_output=True,
            text=True
        )
        print(f"[DEBUG] CLI subprocess stdout: {result.stdout.strip()}")
        print(f"[DEBUG] CLI subprocess stderr: {result.stderr.strip()}")
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
# 🟩 GREEN PATCH 2: FIFO Matching for One-to-One Flattening Only
# ==========================

def fifo_match_and_flatten(active_trades):
    """
    Match each FLATTENING_SELL with the oldest LONG_ENTRY trade (1 contract).
    Match each FLATTENING_BUY with the oldest SHORT_ENTRY trade (1 contract).
    Mark both matched trades as exited and closed.
    """

    # Get flattening sells and long entries
    flattening_sells = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_SELL' and not t.get('exited')]
    long_entries = [t for t in active_trades if t.get('trade_type') == 'LONG_ENTRY' and not t.get('exited')]

    # Get flattening buys and short entries
    flattening_buys = [t for t in active_trades if t.get('trade_type') == 'FLATTENING_BUY' and not t.get('exited')]
    short_entries = [t for t in active_trades if t.get('trade_type') == 'SHORT_ENTRY' and not t.get('exited')]

    print(f"[DEBUG] FIFO Matching: flattening_sells={len(flattening_sells)}, long_entries={len(long_entries)}, flattening_buys={len(flattening_buys)}, short_entries={len(short_entries)}")

    for t in flattening_sells + long_entries + flattening_buys + short_entries:
        print(f"  Trade {t.get('trade_id')} type={t.get('trade_type')} exited={t.get('exited')}")

    # Sort all lists by entry_timestamp ascending or fallback trade_id
    def sort_key(t):
        return t.get('entry_timestamp') or t.get('trade_id')

    flattening_sells.sort(key=sort_key)
    long_entries.sort(key=sort_key)
    flattening_buys.sort(key=sort_key)
    short_entries.sort(key=sort_key)

    # Match flattening sells to long entries one-to-one
    for sell_trade, long_trade in zip(flattening_sells, long_entries):
        sell_trade['exited'] = True
        sell_trade['status'] = 'closed'
        sell_trade['trade_state'] = 'closed'
        sell_trade['contracts_remaining'] = 0

        long_trade['exited'] = True
        long_trade['status'] = 'closed'
        long_trade['trade_state'] = 'closed'
        long_trade['contracts_remaining'] = 0

        print(f"🟢 Matched FLATTENING_SELL {sell_trade['trade_id']} with LONG_ENTRY {long_trade['trade_id']}")

    # Match flattening buys to short entries one-to-one
    for buy_trade, short_trade in zip(flattening_buys, short_entries):
        buy_trade['exited'] = True
        buy_trade['status'] = 'closed'
        buy_trade['trade_state'] = 'closed'
        buy_trade['contracts_remaining'] = 0

        short_trade['exited'] = True
        short_trade['status'] = 'closed'
        short_trade['trade_state'] = 'closed'
        short_trade['contracts_remaining'] = 0

        print(f"🟢 Matched FLATTENING_BUY {buy_trade['trade_id']} with SHORT_ENTRY {short_trade['trade_id']}")

# ==========================
# 🟩 GREEN PATCH: Archive and Delete Matched Trades Immediately After FIFO Matching
# ==========================

def archive_and_delete_matched_trades(symbol, matched_trades):
    for trade in matched_trades:
        trade_id = trade.get('trade_id')
        if not trade_id:
            continue

        # Mark trade as closed in-memory (if not already)
        trade['exited'] = True
        trade['status'] = 'closed'
        trade['trade_state'] = 'closed'
        trade['contracts_remaining'] = 0

        # Archive to Firebase
        archived_ref = db.reference(f"/archived_trades_log/{trade_id}")
        try:
            archived_ref.set(trade)
            print(f"✅ Archived trade {trade_id}")
        except Exception as e:
            print(f"❌ Failed to archive trade {trade_id}: {e}")

        # Delete from open_active_trades
        open_ref = db.reference(f"/open_active_trades/{symbol}/{trade_id}")
        try:
            open_ref.delete()
            print(f"✅ Deleted trade {trade_id} from open_active_trades")
        except Exception as e:
            print(f"❌ Failed to delete trade {trade_id}: {e}")

# Usage (example):
# matched_trades = [list of trades matched in fifo_match_and_flatten()]
# archive_and_delete_matched_trades(symbol, matched_trades)

# ==========================
# 🟩 END FIFO MATCHING PATCH
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
# 🟩 GREEN PATCH 3: Reintegrate check_live_positions_freshness() Function
# ==========================

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
        last_updated_str = last_updated_str.replace(" NZST", "")
        last_updated = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M:%S")
        last_updated = nz_tz.localize(last_updated)
    except Exception as e:
        print(f"⚠️ Failed to parse last_updated timestamp: {e}")
        return False

    now_nz = datetime.now(nz_tz)
    delta_seconds = (now_nz - last_updated).total_seconds()

    print(f"[DEBUG] Current time: {datetime.now(timezone('Pacific/Auckland'))}")
    print(f"[DEBUG] /live_total_positions last_updated: {last_updated} (NZST)")
    print(f"[DEBUG] Data age (seconds): {delta_seconds:.1f}")
    print(f"[DEBUG] Position count: {position_count}")

    if delta_seconds > grace_period_seconds:
        print(f"⚠️ Firebase data too old ({delta_seconds:.1f}s), skipping zombie check")
        return False

    if position_count == 0 or position_count == 0.0:
        print("✅ Position count is zero, safe to run zombie trade detection")
        return True
    else:
        print("⚠️ Position count non-zero, skipping zombie detection to avoid false positives")
        return False

# ==========================
# 🟩 END PATCH 3
# ==========================

# ==========================
# 🟩 GREEN PATCH START: Zombie & Ghost Trade Handler with 30s Grace Period
# ==========================

ZOMBIE_COOLDOWN_SECONDS = 30
GHOST_GRACE_PERIOD_SECONDS = 30
ZOMBIE_STATUSES = {"FILLED"}  # Legitimate filled trades with no position
GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
GRACE_PERIOD_SECONDS = 140 

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
                trade["symbol"] = symbol
                zombie_trades_ref.child(trade_id).set(trade)

                open_trades_ref.child(symbol).child(trade_id).delete()
                print(f"🗑️ Deleted zombie trade {trade_id} from /open_active_trades()")

# ==========================
# 🟩 GREEN PATCH END
# ==========================

def monitor_trades():
    exit_in_progress = set()
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

        # ===== END OF PART 1 & 2 =====

#=========================  MONITOR_TRADES_LOOP - PART 3 (FINAL PART)  ================================

    updated_trades = []
    prices = load_live_prices()

    fifo_match_and_flatten(active_trades)

    matched_trades = [t for t in active_trades if t.get('exited') or t.get('trade_state') == 'closed']
    archive_and_delete_matched_trades(symbol, matched_trades)

    active_trades = [t for t in active_trades if t not in matched_trades]

    save_open_trades(symbol, active_trades)

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

        # 🟢 Trailing TP Exit Handling with exit_in_progress flag (deferred archive/delete)
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

                # Send exit order but DO NOT archive/delete yet
                print(f"[DEBUG] Trailing TP exit triggered for trade {trade_id} with action '{trade['action']}'")
                close_position(symbol, trade['action'])

                # Mark trade as exit in progress
                trade['exit_in_progress'] = True
                updated_trades.append(trade)
                continue

        print(f"📌 Keeping trade {trade.get('trade_id')} OPEN – trail_hit={trade.get('trail_hit')}, exited={trade.get('exited')}, status={trade.get('status')}")
        updated_trades.append(trade)

    # ✅ Only save valid open trades back to Firebase (IF LIGIT TRADES NO LONGER WORK REMOVE THIS AND OPEN tHE COMMENtED OUt VERSION BELOW)
    filtered_trades = []
    for t in updated_trades:
        trade_id = t.get('trade_id', 'unknown')
        exited = t.get('exited', True)
        status = t.get('status', 'closed')
        trade_state = t.get('trade_state', 'closed')
        contracts_remaining = t.get('contracts_remaining', 0)
        filled = t.get('filled', False)
        is_open = t.get('is_open', False)
        liquidation = t.get('liquidation', False)

        if (not exited
            and status != 'closed'
            and trade_state != 'closed'
            and contracts_remaining > 0
            and filled
            and is_open
            and not liquidation):
            filtered_trades.append(t)

    save_open_trades(symbol, filtered_trades)

    # ✅ Only save valid open trades back to Firebase
    # filtered_trades = [
    #     t for t in updated_trades
    #     if not t.get('exited')
    #     and t.get('status') != 'closed'
    #     and t.get('trade_state') != 'closed'
    #     and t.get('contracts_remaining', 0) > 0
    #     and t.get('filled')
    # ]
    # save_open_trades(symbol, filtered_trades)

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

    #=====  END OF PART 3 (END OF SCRIPT)  =====